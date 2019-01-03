# coding: utf-8
import cgi
import json
import urllib
import logging
import time
import httplib
import types
import datetime
from Cookie import SimpleCookie
import traceback
try:
    from cStringIO import StringIO
except ImportError:
    import StringIO

log = logging.getLogger()

version = '1.1'

HTTP_STATUS_CODES = httplib.responses

class Request(object):
    _input = None
    _files = None

    def __init__(self, environ):

        self.environ = environ
        # FIXME: 兼容部分app提交header错误的处理
        if self.environ.has_key('CONTENT_TYPE') and self.environ['CONTENT_TYPE'] == 'application/x-www-form-urlencoded,application/x-www-form-urlencoded; charset=UTF-8':
            self.environ['CONTENT_TYPE'] = 'application/x-www-form-urlencoded; charset=UTF-8'

        # 处理query_string 为cgi提供安全数据
        safe_environ = {'QUERY_STRING':''}
        for key in ('REQUEST_METHOD', 'CONTENT_TYPE', 'CONTENT_LENGTH'):
            if key in self.environ: safe_environ[key] = self.environ[key]
        self.method  = environ.get('REQUEST_METHOD', '')
        self.path    = environ.get('PATH_INFO', '')
        self.host    = environ.get('HTTP_HOST', '')
        self.cookie  = {}
        self.query_string = environ.get('QUERY_STRING', '')
        self.length  = int(environ.get('CONTENT_LENGTH') or '0')
        if self.length:
            self.data = environ['wsgi.input'].read(self.length)
        else:
            self.data = ''

        self._parse_cookie()

        if self.method != 'OPTIONS':
            self.storage = cgi.FieldStorage(fp=StringIO(self.data), environ=safe_environ, keep_blank_values=True)
        else:
            self.storage = None

    def _parse_cookie(self):
        cookiestr = self.environ.get('HTTP_COOKIE', '')
        if not cookiestr:
            return
        cookies = SimpleCookie(cookiestr)
        for c in cookies.values():
            self.cookie[c.key] = c.value

    def _parse_query_string(self):
        qs = self.query_string
        r = {}
        for pair in qs.replace(';','&').split('&'):
            if not pair:
                continue
            nv = pair.split('=', 1)
            if len(nv) != 2:
                nv.append('')
            key = urllib.unquote_plus(nv[0])
            value = urllib.unquote_plus(nv[1])
            r[key] = value
        return r

    def headers(self):
        headers = {}
        cgikeys = ('CONTENT_TYPE', 'CONTENT_LENGTH')

        for i in self.environ:
            if i in cgikeys:
                headers[i.replace('_', '-').title()] = self.environ[i]
            elif i[:5] == 'HTTP_':
                headers[i[5:].replace('_', '-').title()] = self.environ[i]

        return headers

    def clientip(self):
        if 'HTTP_X_FORWARDED_FOR' in self.environ:
            addr = self.environ['HTTP_X_FORWARDED_FOR'].split(',')
            return addr[0]
        return self.environ['REMOTE_ADDR']

    def input(self):
        if self._input:
            return self._input
        data = self._parse_query_string()
        if self.storage is not None  and self.storage.list:
            for k in self.storage.list:
                if k.filename:
                    data[k.name] = k.file
                else:
                    data[k.name] = k.value
        self._input = data
        return self._input

    def postdata(self):
        return self.data

    def inputjson(self):
        data = self.input()
        if self.storage is not None:
            postdata = self.storage.value
            if postdata and postdata[0] == '{' and postdata[-1] == '}':
                try:
                    obj = json.loads(postdata)
                    data.update(obj)
                    self._input = data
                except Exception, e:
                    log.warning('json load error:%s', e)
        return data

    def files(self):
        if self._files:
            return self._files
        data = []
        if self.storage is not None and self.storage.list:
            for k in self.storage.list:
                if k.filename:
                    data.append(k)
                    k.file.seek(0)
        self._files = data
        return self._files



class Response(object):
    def __init__(self, content='', status=200, mimetype='text/html', charset='utf-8'):
        self.content = content
        self.status  = status
        self.mimetype= mimetype
        self.headers = {'X-Powered-By':'QF/'+version}
        self.cookies = SimpleCookie()
        self.charset = charset

        self.headers['Content-Type'] = '%s; charset=%s' % (self.mimetype, self.charset)

    # TODO secure 没有实现
    def set_cookie(self, key, value='', secure=None, **options):
        '''
        option : max_age, expires, path, domain, httponly
        '''
        self.cookies[key] = value
        self.cookies[key]['path'] = '/'

        for k, v in options.items():
            if v:
                if k == 'expires':
                    if isinstance(v, (datetime.date, datetime.datetime)):
                        v = v.timetuple()
                    elif isinstance(v, (int, float)):
                        v = time.gmtime(v)
                    v = time.strftime("%a, %d %b %Y %H:%M:%S GMT", v)
                self.cookies[key][k.replace('_', '-')] = v

    def del_cookie(self, key, **kwargs):
        kwargs['max_age'] = -1
        kwargs['expires'] = 0
        self.set_cookie(key, '', **kwargs)

    def write(self, data):
        if type(data) == types.UnicodeType:
            self.content += data.encode(self.charset)
        else:
            self.content += data

    def length(self):
        return len(self.content)

    def redirect(self, url):
        url = url.encode(self.charset) if isinstance(url,unicode) else str(url)
        self.status = 302
        self.headers['Location'] = url

    def __call__(self, environ, start_response):
        statusstr = '%d %s' % (self.status, HTTP_STATUS_CODES.get(self.status, ''))
        self.headers['Content-Length'] = str(len(self.content))

        headers = self.headers.items()
        # add cookie
        if self.cookies:
            for c in self.cookies.values():
                headers.append(('Set-Cookie', c.OutputString()))

        start_response(statusstr, headers)
        return [self.content]


class ChunkedResponse(Response):
    """生成器方式返回数据的 response, 用来实现 chunked 方式下载.
    由于大部分框架 (gunicorn, uwsgi) 都处理了 chunked 编码, 因此无需重复实现.
    响应数据必须通过调用 callback() 返回, 直接调用 write() 会忽略掉.
    """

    def __init__(self, *args, **kwargs):
        super(ChunkedResponse, self).__init__(*args, **kwargs)
        self._callback = lambda: []

    def write(self, *args, **kwargs):
        pass

    def set_callback(self, cb):
        # cb() 应该返回一个 iterable.
        self._callback = cb

    def __call__(self, environ, start_response):
        try:
            statusstr = '%d %s' % (self.status, HTTP_STATUS_CODES.get(self.status, ''))

            headers = self.headers.items()
            # add cookie
            if self.cookies:
                for c in self.cookies.values():
                    headers.append(('Set-Cookie', c.OutputString()))

            start_response(statusstr, headers)

            for chunk in self._callback():
                yield chunk
        except Exception:
            log.warn(traceback.format_exc())


def NotFound(s=None):
    if not s:
        return Response(HTTP_STATUS_CODES[404], 404)
    return Response(s, 404)

def EmptyGif():
    resp = Response(HTTP_STATUS_CODES[200], 200, 'image/gif')
    resp.write('GIF89a\x01\x00\x01\x00\x80\x01\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x01\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02L\x01\x00;')
    return resp

def MethodNotAllowed():
    return Response(HTTP_STATUS_CODES[405], 405)

def redirect(url, status=302):
    resp = Response('redirect to:%s' % url, status, mimetype='text/html')
    resp.headers['Location'] = url
    return resp

def redirect_referer(req):
    referer = req.environ["HTTP_REFERER"]
    domain  = req.environ["HTTP_HOST"]
    pos = referer.find('/', 7)
    if pos > 0:
        s = referer[pos:]
    else:
        s = '/'
    if s.startswith("/index.py/"):
        s = s[s.find('/', 1):]
    return redirect('http://%s%s' % (domain, s))



