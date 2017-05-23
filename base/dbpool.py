# coding: utf-8
import time, datetime, os
import types, random
import threading
import logging
import traceback
import pager
from contextlib import contextmanager
log = logging.getLogger()

dbpool = None


def timeit(func):
    def _(*args, **kwargs):
        starttm = time.time()
        ret = 0
        num = 0
        err = ''
        try:
            retval = func(*args, **kwargs)
            t = type(retval)
            if t == types.ListType:
                num = len(retval)
            elif t == types.DictType:
                num = 1
            elif t == types.IntType:
                ret = retval
            return retval
        except Exception, e:
            err = str(e)
            ret = -1
            raise
        finally:
            endtm = time.time()
            conn = args[0]
            #dbcf = conn.pool.dbcf
            dbcf = conn.param
            log.info('server=%s|name=%s|user=%s|addr=%s:%d|db=%s|idle=%d|busy=%d|max=%d|time=%d|ret=%s|num=%d|sql=%s|err=%s',
                     conn.type, conn.name, dbcf.get('user',''),
                     dbcf.get('host',''), dbcf.get('port',0),
                     os.path.basename(dbcf.get('db','')),
                     len(conn.pool.dbconn_idle),
                     len(conn.pool.dbconn_using),
                     conn.pool.max_conn,
                     int((endtm-starttm)*1000000),
                     str(ret), num, repr(args[1]), err)
    return _


class DBPoolBase:
    def acquire(self, name):
        pass

    def release(self, name, conn):
        pass


class DBResult:
    def __init__(self, fields, data):
        self.fields = fields
        self.data = data

    def todict(self):
        ret = []
        for item in self.data:
            ret.append(dict(zip(self.fields, item)))
        return ret

    def __iter__(self):
        for row in self.data:
            yield dict(zip(self.fields, row))

    def row(self, i, isdict=True):
        if isdict:
            return dict(zip(self.fields, self.data[i]))
        return self.data[i]

    def __getitem__(self, i):
        return dict(zip(self.fields, self.data[i]))

class DBFunc:
    def __init__(self, data):
        self.value = data


class DBConnection:
    def __init__(self, param, lasttime, status):
        self.name       = None
        self.param      = param
        self.conn       = None
        self.status     = status
        self.lasttime   = lasttime
        self.pool       = None
        self.server_id  = None

    def __str__(self):
        return '<%s %s:%d %s@%s>' % (self.type,
                self.param.get('host',''), self.param.get('port',0),
                self.param.get('user',''), self.param.get('db',0)
                )

    def is_available(self):
        return self.status == 0

    def useit(self):
        self.status = 1
        self.lasttime = time.time()

    def releaseit(self):
        self.status = 0

    def connect(self):
        pass

    def close(self):
        pass

    def alive(self):
        pass

    def cursor(self):
        return self.conn.cursor()

    @timeit
    def execute(self, sql, param=None):
        #log.info('exec:%s', sql)
        cur = self.conn.cursor()
        if param:
            if not isinstance(param, (types.DictType, types.TupleType)):
                param = tuple([param])
            ret = cur.execute(sql, param)
        else:
            ret = cur.execute(sql)
        cur.close()
        return ret

    @timeit
    def executemany(self, sql, param):
        cur = self.conn.cursor()
        if param:
            if not isinstance(param, (types.DictType, types.TupleType)):
                param = tuple([param])
            ret = cur.executemany(sql, param)
        else:
            ret = cur.executemany(sql)
        cur.close()
        return ret

    @timeit
    def query(self, sql, param=None, isdict=True, head=False):
        '''sql查询，返回查询结果'''
        #log.info('query:%s', sql)
        cur = self.conn.cursor()
        if not param:
            cur.execute(sql)
        else:
            if not isinstance(param, (types.DictType, types.TupleType)):
                param = tuple([param])
            cur.execute(sql, param)
        res = cur.fetchall()
        cur.close()
        res = [self.format_timestamp(r, cur) for r in res]
        #log.info('desc:', cur.description)
        if res and isdict:
            ret = []
            xkeys = [ i[0] for i in cur.description]
            for item in res:
                ret.append(dict(zip(xkeys, item)))
        else:
            ret = res
            if head:
                xkeys = [ i[0] for i in cur.description]
                ret.insert(0, xkeys)
        return ret

    @timeit
    def get(self, sql, param=None, isdict=True):
        '''sql查询，只返回一条'''
        cur = self.conn.cursor()
        if not param:
            cur.execute(sql)
        else:
            if not isinstance(param, (types.DictType, types.TupleType)):
                param = tuple([param])
            cur.execute(sql, param)
        res = cur.fetchone()
        cur.close()
        res = self.format_timestamp(res, cur)
        if res and isdict:
            xkeys = [ i[0] for i in cur.description]
            return dict(zip(xkeys, res))
        else:
            return res

    def value2sql(self, v, charset='utf-8'):
        tv = type(v)
        if tv in [types.StringType, types.UnicodeType]:
            if tv == types.UnicodeType:
                v = v.encode(charset)
            if v.startswith(('now()','md5(')):
                return v
            return "'%s'" % self.escape(v)
        elif isinstance(v, datetime.datetime) or isinstance(v, datetime.date):
            return "'%s'" % str(v)
        elif isinstance(v, DBFunc):
            return v.value
        else:
            if v is None:
                return 'NULL'
            return str(v)

    def exp2sql(self, key, op, value):
        item = '(`%s` %s ' % (key.strip('`').replace('.','`.`'), op)
        if op == 'in':
            item += '(%s))' % ','.join([self.value2sql(x) for x in value])
        elif op == 'not in':
            item += '(%s))' % ','.join([self.value2sql(x) for x in value])
        elif op == 'between':
            item += ' %s and %s)' % (self.value2sql(value[0]), self.value2sql(value[1]))
        else:
            item += self.value2sql(value) + ')'
        return item

    def dict2sql(self, d, sp=','):
        '''字典可以是 {name:value} 形式，也可以是 {name:(operator, value)}'''
        x = []
        for k,v in d.iteritems():
            if isinstance(v, types.TupleType):
                x.append('%s' % self.exp2sql(k, v[0], v[1]))
            else:
                x.append('`%s`=%s' % (k.strip(' `').replace('.','`.`'), self.value2sql(v)))
        return sp.join(x)

    def dict2on(self, d, sp=' and '):
        x = []
        for k,v in d.iteritems():
            x.append('`%s`=`%s`' % (k.strip(' `').replace('.','`.`'), v.strip(' `').replace('.','`.`')))
        return sp.join(x)

    def dict2insert(self, d):
        keys = d.keys()
        vals = []
        for k in keys:
            vals.append('%s' % self.value2sql(d[k]))
        new_keys = ['`' + k.strip('`') + '`' for k in keys]
        return ','.join(new_keys), ','.join(vals)

    def fields2where(self, fields, where=None):
        if not where:
            where = {}
        for f in fields:
            if f.value == None or (f.value == '' and f.isnull == False):
                continue
            where[f.name] = (f.op, f.value)
        return where

    def format_table(self, table):
        '''调整table 支持加上 `` 并支持as'''
        #如果有as
        table = table.strip(' `').replace(',','`,`')
        index = table.find(' ')
        if ' ' in table:
            return '`%s`%s' % ( table[:index] ,table[index:])
        else:
            return '`%s`' % table

    def insert(self, table, values, other=None):
        #sql = "insert into %s set %s" % (table, self.dict2sql(values))
        keys, vals = self.dict2insert(values)
        sql = "insert into %s(%s) values (%s)" % (self.format_table(table), keys, vals)
        if other:
            sql += ' ' + other
        return self.execute(sql)

    def insert_list(self, table, values_list, other=None):
        sql = 'insert into %s ' % self.format_table(table)
        sql_key = ''
        sql_value = []
        for values in values_list:
            keys, vals = self.dict2insert(values)
            sql_key = keys  # 正常key肯定是一样的
            sql_value.append('(%s)' % vals)
        sql += ' (' + sql_key + ') ' + 'values' + ','.join(sql_value)
        if other:
            sql += ' ' + other
        return self.execute(sql)

    def update(self, table, values, where=None, other=None):
        sql = "update %s set %s" % (self.format_table(table), self.dict2sql(values))
        if where:
            sql += " where %s" % self.dict2sql(where,' and ')
        if other:
            sql += ' ' + other
        return self.execute(sql)

    def delete(self, table, where, other=None):
        sql = "delete from %s" % self.format_table(table)
        if where:
            sql += " where %s" % self.dict2sql(where, ' and ')
        if other:
            sql += ' ' + other
        return self.execute(sql)

    def select(self, table, where=None, fields='*', other=None, isdict=True):
        sql = self.select_sql(table, where, fields, other)
        return self.query(sql, None, isdict=isdict)

    def select_one(self, table, where=None, fields='*', other=None, isdict=True):
        if not other:
            other = ' limit 1'
        if 'limit' not in other:
            other += ' limit 1'

        sql = self.select_sql(table, where, fields, other)
        return self.get(sql, None, isdict=isdict)

    def select_join(self, table1, table2, join_type='inner', on=None, where=None, fields='*', other=None, isdict=True):
        sql = self.select_join_sql(table1, table2, join_type, on, where, fields, other)
        return self.query(sql, None, isdict=isdict)

    def select_join_one(self, table1, table2, join_type='inner', on=None, where=None, fields='*', other=None, isdict=True):
        if not other:
            other = ' limit 1'
        if 'limit' not in other:
            other += ' limit 1'

        sql = self.select_join_sql(table1, table2, join_type, on, where, fields, other)
        return self.get(sql, None, isdict=isdict)

    def select_sql(self, table, where=None, fields='*', other=None):
        if type(fields) in (types.ListType, types.TupleType):
            fields = ','.join(fields)
        sql = "select %s from %s" % (fields, self.format_table(table))
        if where:
            sql += " where %s" % self.dict2sql(where, ' and ')
        if other:
            sql += ' ' + other
        return sql

    def select_join_sql(self, table1, table2, join_type='inner', on=None, where=None, fields='*', other=None):
        if type(fields) in (types.ListType, types.TupleType):
            fields = ','.join(fields)
        sql = "select %s from %s %s join %s" % (fields, self.format_table(table1), join_type, self.format_table(table2))
        if on:
            sql += " on %s" % self.dict2on(on, ' and ')
        if where:
            sql += " where %s" % self.dict2sql(where, ' and ')
        if other:
            sql += ' ' + other
        return sql

    def select_page(self, sql, pagecur=1, pagesize=20, count_sql=None, maxid=-1):
        return pager.db_pager(self, sql, pagecur, pagesize, count_sql, maxid)

    def last_insert_id(self):
        pass

    def start(self): # start transaction
        pass

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def escape(self, s):
        return s

    def format_timestamp(self, ret, cur):
        '''将字段以_time结尾的格式化成datetime'''
        if not ret:
            return ret
        index = []
        for d in cur.description:
            if d[0].endswith('_time'):
                index.append(cur.description.index(d))

        res = []
        for i , t in enumerate(ret):
            if i in index and type(t) in [types.IntType,types.LongType]:
                res.append(datetime.datetime.fromtimestamp(t))
            else:
                res.append(t)
        return res

def with_mysql_reconnect(func):

    def close_mysql_conn(self):
        try:
            self.conn.close()
        except:
            log.warning(traceback.format_exc())
            self.conn = None

    def _(self, *args, **argitems):
        if self.type == 'mysql':
            import MySQLdb as m
        elif self.type == 'pymysql':
            import pymysql as m
        trycount = 3
        while True:
            try:
                x = func(self, *args, **argitems)
            except m.OperationalError, e:
                log.warning(traceback.format_exc())
                if e[0] >= 2000: # 客户端错误
                    close_mysql_conn(self)
                    self.connect()
                    trycount -= 1
                    if trycount > 0:
                        continue
                raise
            except m.InterfaceError, e:
                log.warning(traceback.format_exc())
                close_mysql_conn(self)
                self.connect()
                trycount -= 1
                if trycount > 0:
                    continue
                raise
            else:
                return x
    return _



#def with_mysql_reconnect(func):
#    def _(self, *args, **argitems):
#        import MySQLdb
#        trycount = 3
#        while True:
#            try:
#                x = func(self, *args, **argitems)
#            except MySQLdb.OperationalError, e:
#                #log.err('mysql error:', e)
#                if e[0] >= 2000: # client error
#                    #log.err('reconnect ...')
#                    self.conn.close()
#                    self.connect()
#
#                    trycount -= 1
#                    if trycount > 0:
#                        continue
#                raise
#            else:
#                return x
#
#    return _




class MySQLConnection (DBConnection):
    type = "mysql"
    def __init__(self, param, lasttime, status):
        DBConnection.__init__(self, param, lasttime, status)

        self.connect()

    def useit(self):
        self.status = 1
        self.lasttime = time.time()

    def releaseit(self):
        self.status = 0

    def connect(self):
        engine = self.param['engine']
        if engine == 'mysql':
            import MySQLdb
            self.conn = MySQLdb.connect(host = self.param['host'],
                                        port = self.param['port'],
                                        user = self.param['user'],
                                        passwd = self.param['passwd'],
                                        db = self.param['db'],
                                        charset = self.param['charset'],
                                        connect_timeout = self.param.get('timeout', 10),
                                        )

            self.conn.autocommit(1)

            cur = self.conn.cursor()
            cur.execute("show variables like 'server_id'")
            row = cur.fetchone()
            self.server_id = int(row[1])
            #if self.param.get('autocommit',None):
            #    log.note('set autocommit')
            #    self.conn.autocommit(1)
            #initsqls = self.param.get('init_command')
            #if initsqls:
            #    log.note('init sqls:', initsqls)
            #    cur = self.conn.cursor()
            #    cur.execute(initsqls)
            #    cur.close()
        else:
            raise ValueError, 'engine error:' + engine
        #log.note('mysql connected', self.conn)

    def close(self):
        self.conn.close()
        self.conn = None

    @with_mysql_reconnect
    def alive(self):
        if self.is_available():
            cur = self.conn.cursor()
            cur.execute("show tables;")
            cur.close()
            self.conn.ping()

    @with_mysql_reconnect
    def execute(self, sql, param=None):
        return DBConnection.execute(self, sql, param)

    @with_mysql_reconnect
    def executemany(self, sql, param):
        return DBConnection.executemany(self, sql, param)

    @with_mysql_reconnect
    def query(self, sql, param=None, isdict=True, head=False):
        return DBConnection.query(self, sql, param, isdict, head)

    @with_mysql_reconnect
    def get(self, sql, param=None, isdict=True):
        return DBConnection.get(self, sql, param, isdict)

    def escape(self, s, enc='utf-8'):
        if type(s) == types.UnicodeType:
            s = s.encode(enc)
        ns = self.conn.escape_string(s)
        return unicode(ns, enc)

    def last_insert_id(self):
        ret = self.query('select last_insert_id()', isdict=False)
        return ret[0][0]

    def start(self):
        sql = "start transaction"
        return self.execute(sql)

    def commit(self):
        sql = 'commit'
        return self.execute(sql)

    def rollback(self):
        sql = 'rollback'
        return self.execute(sql)

class PyMySQLConnection (MySQLConnection):
    type = "pymysql"
    def __init__(self, param, lasttime, status):
        MySQLConnection.__init__(self, param, lasttime, status)

    def connect(self):
        engine = self.param['engine']
        if engine == 'pymysql':
            import pymysql
            self.conn = pymysql.connect(host = self.param['host'],
                                        port = self.param['port'],
                                        user = self.param['user'],
                                        passwd = self.param['passwd'],
                                        db = self.param['db'],
                                        charset = self.param['charset'],
                                        connect_timeout = self.param.get('timeout', 10),
                                        )
            self.conn.autocommit(1)

            cur = self.conn.cursor()
            cur.execute("show variables like 'server_id'")
            row = cur.fetchone()
            self.server_id = int(row[1])
        else:
            raise ValueError, 'engine error:' + engine

class SQLiteConnection (DBConnection):
    type = "sqlite"
    def __init__(self, param, lasttime, status):
        DBConnection.__init__(self, param, lasttime, status)

    def connect(self):
        engine = self.param['engine']
        if engine == 'sqlite':
            import sqlite3
            self.conn = sqlite3.connect(self.param['db'], detect_types=sqlite3.PARSE_DECLTYPES, isolation_level=None)
        else:
            raise ValueError, 'engine error:' + engine

    def useit(self):
        DBConnection.useit(self)
        if not self.conn:
            self.connect()

    def releaseit(self):
        DBConnection.releaseit(self)
        self.conn.close()
        self.conn = None

    def escape(self, s, enc='utf-8'):
        s = s.replace("'", "\'")
        s = s.replace('"', '\"')
        return s

    def last_insert_id(self):
        ret = self.query('select last_insert_rowid()', isdict=False)
        return ret[0][0]

    def start(self):
        sql = "BEGIN"
        return self.conn.execute(sql)



class DBPool (DBPoolBase):
    def __init__(self, dbcf):
        # one item: [conn, last_get_time, stauts]
        self.dbconn_idle  = []
        self.dbconn_using = []

        self.dbcf   = dbcf
        self.max_conn = 20
        self.min_conn = 1

        if self.dbcf.has_key('conn'):
            self.max_conn = self.dbcf['conn']

        self.connection_class = {}
        x = globals()
        for v in x.itervalues():
            if type(v) == types.ClassType and v != DBConnection and issubclass(v, DBConnection):
                self.connection_class[v.type] = v

        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)

        self.open(self.min_conn)

    def synchronize(func):
        def _(self, *args, **argitems):
            self.lock.acquire()
            x = None
            try:
                x = func(self, *args, **argitems)
            finally:
                self.lock.release()
            return x
        return _

    def open(self, n=1):
        param = self.dbcf
        newconns = []
        for i in range(0, n):
            myconn = self.connection_class[param['engine']](param, time.time(), 0)
            myconn.pool = self
            newconns.append(myconn)
        self.dbconn_idle += newconns

    def clear_timeout(self):
        #log.info('try clear timeout conn ...')
        now = time.time()
        dels = []
        allconn = len(self.dbconn_idle) + len(self.dbconn_using)
        for c in self.dbconn_idle:
            if allconn == 1:
                break
            if now - c.lasttime > self.dbcf.get('idle_timeout', 10):
                dels.append(c)
                allconn -= 1

        if dels:
            log.debug('close timeout db conn:%d', len(dels))
        for c in dels:
            if c.conn:
                c.close()
            self.dbconn_idle.remove(c)

    @synchronize
    def acquire(self, timeout=10):
        start = time.time()
        while len(self.dbconn_idle) == 0:
            if len(self.dbconn_idle) + len(self.dbconn_using) < self.max_conn:
                self.open()
                continue
            self.cond.wait(timeout)
            if int(time.time() - start) > timeout:
                log.error('func=acquire|error=no idle connections')
                raise RuntimeError('no idle connections')

        conn = self.dbconn_idle.pop(0)
        conn.useit()
        self.dbconn_using.append(conn)

        if random.randint(0, 100) > 80:
            try:
                self.clear_timeout()
            except:
                log.error(traceback.format_exc())

        return conn

    @synchronize
    def release(self, conn):
        # conn是有效的
        # FIXME: conn有可能为false吗？这样是否会有conn从dbconn_using里出不来了
        if conn:
            self.dbconn_using.remove(conn)
            conn.releaseit()
            if conn.conn:
                self.dbconn_idle.insert(0, conn)
        self.cond.notify()


    @synchronize
    def alive(self):
        for conn in self.dbconn_idle:
            conn.alive()

    def size(self):
        return len(self.dbconn_idle), len(self.dbconn_using)


class DBConnProxy:
    #def __init__(self, masterconn, slaveconn):
    def __init__(self, pool, timeout=10):
        #self.name   = ''
        #self.master = masterconn
        #self.slave  = slaveconn

        self._pool = pool
        self._master = None
        self._slave = None
        self._timeout = timeout

        self._modify_methods = set(['execute', 'executemany', 'last_insert_id',
                'insert', 'update', 'delete', 'insert_list', 'start', 'rollback', 'commit'])

    def __getattr__(self, name):
        #if name.startswith('_') and name[1] != '_':
        #    return self.__dict__[name]
        if name in self._modify_methods:
            if not self._master:
                self._master = self._pool.master.acquire(self._timeout)
            return getattr(self._master, name)
        else:
            if name == 'master':
                if not self._master:
                    self._master = self._pool.master.acquire(self._timeout)
                return self._master
            if name == 'slave':
                if not self._slave:
                    self._slave = self._pool.get_slave().acquire(self._timeout)
                return self._slave

            if not self._slave:
                self._slave = self._pool.get_slave().acquire(self._timeout)
            return getattr(self._slave, name)


class RWDBPool:
    def __init__(self, dbcf):
        self.dbcf   = dbcf
        self.name   = ''
        self.policy = dbcf.get('policy', 'round_robin')
        self.master = DBPool(dbcf.get('master', None))
        self.slaves = []

        self._slave_current = -1

        for x in dbcf.get('slave', []):
            self.slaves.append(DBPool(x))

    def get_slave(self):
        if self.policy == 'round_robin':
            size = len(self.slaves)
            self._slave_current = (self._slave_current + 1) % size
            return self.slaves[self._slave_current]
        else:
            raise ValueError, 'policy not support'

    def get_master(self):
        return self.master

#    def acquire(self, timeout=10):
#        #log.debug('rwdbpool acquire')
#        master_conn = None
#        slave_conn  = None
#
#        try:
#            master_conn = self.master.acquire(timeout)
#            slave_conn  = self.get_slave().acquire(timeout)
#            return DBConnProxy(master_conn, slave_conn)
#        except:
#            if master_conn:
#                master_conn.pool.release(master_conn)
#            if slave_conn:
#                slave_conn.pool.release(slave_conn)
#            raise

    def acquire(self, timeout=10):
        return DBConnProxy(self, timeout)

    def release(self, conn):
        #log.debug('rwdbpool release')
        if conn._master:
            #log.debug('release master')
            conn._master.pool.release(conn._master)
        if conn._slave:
            #log.debug('release slave')
            conn._slave.pool.release(conn._slave)


    def size(self):
        ret = {'master': (-1,-1), 'slave':[]}
        if self.master:
            ret['master'] = self.master.size()
        for x in self.slaves:
            key = '%s@%s:%d' % (x.dbcf['user'], x.dbcf['host'], x.dbcf['port'])
            ret['slave'].append((key, x.size()))
        return ret




def checkalive(name=None):
    global dbpool
    while True:
        if name is None:
            checknames = dbpool.keys()
        else:
            checknames = [name]
        for k in checknames:
            pool = dbpool[k]
            pool.alive()
        time.sleep(300)

def install(cf):
    global dbpool
    if dbpool:
        log.warn("too many install db")
        return dbpool
    dbpool = {}

    for name,item in cf.iteritems():
        #item = cf[name]
        dbp = None
        if item.has_key('master'):
            dbp = RWDBPool(item)
        else:
            dbp = DBPool(item)
        dbpool[name] = dbp
    return dbpool


def acquire(name, timeout=10):
    global dbpool
    #log.info("acquire:", name)
    pool = dbpool[name]
    x = pool.acquire(timeout)
    x.name = name
    return x

def release(conn):
    if not conn:
        return
    global dbpool
    #log.info("release:", name)
    pool = dbpool[conn.name]
    return pool.release(conn)

def execute(db, sql, param=None):
    return db.execute(sql, param)

def executemany(db, sql, param):
    return db.executemany(sql, param)

def query(db, sql, param=None, isdict=True, head=False):
    return db.query(sql, param, isdict, head)

@contextmanager
def get_connection(token):
    try:
        conn = acquire(token)
        yield conn
    except:
        log.error("error=%s", traceback.format_exc())
    finally:
        release(conn)

@contextmanager
def get_connection_exception(token):
    '''出现异常捕获后，关闭连接并抛出异常'''
    try:
        conn = acquire(token)
        yield conn
    except:
        #log.error("error=%s", traceback.format_exc())
        raise
    finally:
        release(conn)


def with_database(name, errfunc=None, errstr=''):
    def f(func):
        def _(self, *args, **argitems):
            multi_db = isinstance(name, (types.TupleType, types.ListType))
            is_none, is_inst = False, False
            if isinstance(self, types.NoneType):
                is_none = True
            elif isinstance(self, types.ObjectType):
                is_inst = True

            if multi_db:
                dbs = {}
                for dbname in name:
                    dbs[dbname] = acquire(dbname)
                if is_inst:
                    self.db = dbs
                elif is_none:
                    self = dbs
            else:
                if is_inst:
                    self.db = acquire(name)
                elif is_none:
                    self = acquire(name)

            x = None
            try:
                x = func(self, *args, **argitems)
            except:
                if errfunc:
                    return getattr(self, errfunc)(error=errstr)
                else:
                    raise
            finally:
                if multi_db:
                    if is_inst:
                        dbs = self.db
                    else:
                        dbs = self
                    dbnames = dbs.keys()
                    for dbname in dbnames:
                        release(dbs.pop(dbname))
                else:
                    if is_inst:
                        release(self.db)
                    elif is_none:
                        release(self)
                if is_inst:
                    self.db = None
                elif is_none:
                    self = None
            return x
        return _
    return f

def test():
    import random, logger
    logger.install('stdout')
    #log.install("SimpleLogger")
    dbcf = {'test1': {'engine': 'sqlite', 'db':'test1.db', 'conn':1}}
    #dbcf = {'test1': {'engine': 'sqlite', 'db':':memory:', 'conn':1}}
    install(dbcf)

    sql = "create table if not exists user(id integer primary key, name varchar(32), ctime timestamp)"
    print 'acquire'
    x = acquire('test1')
    print 'acquire ok'
    x.execute(sql)

    #sql1 = "insert into user values (%d, 'zhaowei', datetime())" % (random.randint(1, 100));
    sql1 = "insert into user values (%d, 'zhaowei', datetime())" % (random.randint(1, 100));
    x.execute(sql1)

    x.insert("user", {"name":"bobo","ctime":DBFunc("datetime()")})

    sql2 = "select * from user"
    ret = x.query(sql2)
    print 'result:', ret

    ret = x.query('select * from user where name=?', 'bobo')
    print 'result:', ret

    print 'release'
    release(x)
    print 'release ok'

    print '-' * 60

    class Test2:
        @with_database("test1")
        def test2(self):
            ret = self.db.query("select * from user")
            print ret

    t = Test2()
    t.test2()


def test1():
    DATABASE = {'test': # connection name, used for getting connection from pool
                {'engine':'mysql',      # db type, eg: mysql, sqlite
                 'db':'test',       # db table
                 'host':'127.0.0.1', # db host
                 'port':3306,        # db port
                 'user':'root',      # db user
                 'passwd':'654321',# db password
                 'charset':'utf8',# db charset
                 'conn':20}          # db connections in pool
           }

    install(DATABASE)

    while True:
        x = random.randint(0, 10)
        print 'x:', x
        conns = []
        for i in range(0, x):
            c = acquire('test')
            time.sleep(1)
            conns.append(c)
            print dbpool['test'].size()

        for c in conns:
            release(c)
            time.sleep(1)
            print dbpool['test'].size()

        time.sleep(1)
        print dbpool['test'].size()

def test2():
    DATABASE = {'test': # connection name, used for getting connection from pool
                {'engine':'mysql',   # db type, eg: mysql, sqlite
                 'db':'test',        # db name
                 'host':'127.0.0.1', # db host
                 'port':3306,        # db port
                 'user':'root',      # db user
                 'passwd':'123456',  # db password
                 'charset':'utf8',   # db charset
                 'conn':110}          # db connections in pool
           }

    install(DATABASE)

    def go():
        #x = random.randint(0, 10)
        #print 'x:', x
        #conns = []
        #for i in range(0, x):
        #    c = acquire('test')
        #    #time.sleep(1)
        #    conns.append(c)
        #    print dbpool['test'].size()

        #for c in conns:
        #    release(c)
        #    #time.sleep(1)
        #    print dbpool['test'].size()

        while True:
            #time.sleep(1)
            c = acquire('test')
            #print dbpool['test'].size()
            release(c)
            #print dbpool['test'].size()

    ths = []
    for i in range(0, 100):
        t = threading.Thread(target=go, args=())
        ths.append(t)

    for t in ths:
        t.start()

    for t in ths:
        t.join()


def test3():
    import logger
    logger.install('stdout')
    global log
    log = logger.log

    DATABASE = {'test':{
                'policy': 'round_robin',
                'default_conn':'auto',
                'master':
                    {'engine':'mysql',
                     'db':'test',
                     'host':'127.0.0.1',
                     'port':3306,
                     'user':'root',
                     'passwd':'123456',
                     'charset':'utf8',
                     'idle_timeout':60,
                     'conn':20},
                'slave':[
                    {'engine':'mysql',
                     'db':'test',
                     'host':'127.0.0.1',
                     'port':3306,
                     'user':'zhaowei_r1',
                     'passwd':'123456',
                     'charset':'utf8',
                     'conn':20},
                    {'engine':'mysql',
                     'db':'test',
                     'host':'127.0.0.1',
                     'port':3306,
                     'user':'zhaowei_r2',
                     'passwd':'123456',
                     'charset':'utf8',
                     'conn':20},
                    ],
                },

           }

    install(DATABASE)

    while True:
        x = random.randint(0, 10)
        print 'x:', x
        conns = []

        print 'acquire ...'
        for i in range(0, x):
            c = acquire('test')
            time.sleep(1)
            c.insert('ztest', {'name':'zhaowei%d'%(i)})
            print c.query('select count(*) from ztest')
            print c.get('select count(*) from ztest')
            conns.append(c)
            print dbpool['test'].size()


        print 'release ...'
        for c in conns:
            release(c)
            time.sleep(1)
            print dbpool['test'].size()

        time.sleep(1)
        print '-'*60
        print dbpool['test'].size()
        print '-'*60
        time.sleep(1)

def test4(tcount):
    DATABASE = {'test': # connection name, used for getting connection from pool
                {'engine':'mysql',   # db type, eg: mysql, sqlite
                 'db':'qf_core',        # db name
                 'host':'172.100.101.106', # db host
                 'port':3306,        # db port
                 'user':'qf',      # db user
                 'passwd':'123456',  # db password
                 'charset':'utf8',   # db charset
                 'conn':10}          # db connections in pool
           }

    install(DATABASE)

    def run_thread():
        while True:
            time.sleep(0.01)
            conn = None
            try:
                conn = acquire('test')
            except:
                log.debug("%s catch exception in acquire", threading.currentThread().name)
                traceback.print_exc()
                time.sleep(0.5)
                continue
            try:
                sql = "select count(*) from profile"
                ret = conn.query(sql)
            except:
                log.debug("%s catch exception in query", threading.currentThread().name)
                traceback.print_exc()
            finally:
                if conn:
                    release(conn)
                    conn = None

    import threading
    th = []
    for i in range(0, tcount):
        _th = threading.Thread(target=run_thread, args=())
        log.debug("%s create", _th.name)
        th.append(_th)

    for t in th:
        t.start()
        log.debug("%s start", t.name)

    for t in th:
        t.join()
        log.debug("%s finish",t.name)


def test5():
    DATABASE = {'test': # connection name, used for getting connection from pool
                {'engine':'mysql',   # db type, eg: mysql, sqlite
                 'db':'qf_core',        # db name
                 'host':'172.100.101.106', # db host
                 'port':3306,        # db port
                 'user':'qf',      # db user
                 'passwd':'123456',  # db password
                 'charset':'utf8',   # db charset
                 'conn':20}          # db connections in pool
           }

    install(DATABASE)

    def run_thread():
        i = 0
        while i < 10:
            time.sleep(0.01)
            with get_connection('test') as conn:
                sql = "select count(*) from profile"
                ret = conn.query(sql)
                log.debug('ret:%s', ret)
            i += 1
        pool = dbpool['test']
        log.debug("pool size: %s", pool.size())
    import threading
    th = []
    for i in range(0, 10):
        _th = threading.Thread(target=run_thread, args=())
        log.debug("%s create", _th.name)
        th.append(_th)

    for t in th:
        t.setDaemon(True)
        t.start()
        log.debug("%s start", t.name)

def test_with():
    DATABASE = {'test': # connection name, used for getting connection from pool
                {'engine':'mysql',   # db type, eg: mysql, sqlite
                 'db':'qf_core',        # db name
                 'host':'172.100.101.106', # db host
                 'port':3306,        # db port
                 'user':'qf',      # db user
                 'passwd':'123456',  # db password
                 'charset':'utf8',   # db charset
                 'conn':10}          # db connections in pool
           }

    install(DATABASE)
    with get_connection('test') as conn:
        record = conn.query("select retcd from profile where userid=227519")
        print record
        #record = conn.query("select * from chnlbind where userid=227519")
        #print record
    pool = dbpool['test']
    print pool.size()

    with get_connection('test') as conn:
        record = conn.query("select * from profile where userid=227519")
        print record
        record = conn.query("select * from chnlbind where userid=227519")
        print record

    pool = dbpool['test']
    print pool.size()
def test_format_time():
    import logger
    logger.install('stdout')
    DATABASE = {'test': # connection name, used for getting connection from pool
                {'engine':'mysql',   # db type, eg: mysql, sqlite
                 'db':'qiantai',        # db name
                 'host':'172.100.101.151', # db host
                 'port':3306,        # db port
                 'user':'qf',      # db user
                 'passwd':'123456',  # db password
                 'charset':'utf8',   # db charset
                 'conn':10}          # db connections in pool
           }

    install(database)
    with get_connection('test') as conn:
        print conn.select('order')
        print conn.select_join('app','customer','inner',)
        print conn.format_table('order as o')

def test_base_func():
    import logger
    logger.install('stdout')
    database = {'test': # connection name, used for getting connection from pool
                {'engine':'mysql',   # db type, eg: mysql, sqlite
                 'db':'qf_core',        # db name
                 'host':'172.100.101.151', # db host
                 'port':3306,        # db port
                 'user':'qf',      # db user
                 'passwd':'123456',  # db password
                 'charset':'utf8',   # db charset
                 'conn':10}          # db connections in pool
           }
    install(database)
    with get_connection('test') as conn:
        conn.insert('auth_user',{
            'username':'13512345677',
            'password':'123',
            'mobile':'13512345677',
            'email':'123@haha.cn',
        })
        print  conn.select('auth_user',{
            'username':'13512345677',
        })
        conn.delete('auth_user',{
            'username':'13512345677',
        })
        conn.select_join('profile as p','auth_user as a',where={
            'p.userid':DBFunc('a.id'),
        })

def test_new_rw():
    import logger
    logger.install('stdout')
    database = {'test':{
                'policy': 'round_robin',
                'default_conn':'auto',
                'master':
                    {'engine':'mysql',
                     'db':'test',
                     'host':'127.0.0.1',
                     'port':3306,
                     'user':'root',
                     'passwd':'654321',
                     'charset':'utf8',
                     'conn':10}
                 ,
                 'slave':[
                    {'engine':'mysql',
                     'db':'test',
                     'host':'127.0.0.1',
                     'port':3306,
                     'user':'root',
                     'passwd':'654321',
                     'charset':'utf8',
                     'conn':10
                    },
                    {'engine':'mysql',
                     'db':'test',
                     'host':'127.0.0.1',
                     'port':3306,
                     'user':'root',
                     'passwd':'654321',
                     'charset':'utf8',
                     'conn':10
                    }

                 ]
             }
            }
    install(database)

    def printt(t=0):
        now = time.time()
        if t > 0:
            print 'time:', now-t
        return now

    t = printt()
    with get_connection('test') as conn:
        t = printt(t)
        print 'master:', conn._master, 'slave:', conn._slave
        assert conn._master == None
        assert conn._slave == None
        ret = conn.query("select 10")
        t = printt(t)
        print 'after read master:', conn._master, 'slave:', conn._slave
        assert conn._master == None
        assert conn._slave != None
        conn.execute('create table if not exists haha (id int(4) not null primary key, name varchar(128) not null)')
        t = printt(t)
        print 'master:', conn._master, 'slave:', conn._slave
        assert conn._master != None
        assert conn._slave != None
        conn.execute('drop table haha')
        t = printt(t)
        assert conn._master != None
        assert conn._slave != None
        print 'ok'

    print '=' * 20
    t = printt()
    with get_connection('test') as conn:
        t = printt(t)
        print 'master:', conn._master, 'slave:', conn._slave
        assert conn._master == None
        assert conn._slave == None

        ret = conn.master.query("select 10")
        assert conn._master != None
        assert conn._slave == None

        t = printt(t)
        print 'after query master:', conn._master, 'slave:', conn._slave
        ret = conn.query("select 10")
        assert conn._master != None
        assert conn._slave != None

        print 'after query master:', conn._master, 'slave:', conn._slave
        print 'ok'

def test_db_install():
    import logger
    logger.install('stdout')
    DATABASE = {'test': # connection name, used for getting connection from pool
                {'engine':'pymysql',   # db type, eg: mysql, sqlite
                 'db':'qiantai',        # db name
                 'host':'172.100.101.156', # db host
                 'port':3306,        # db port
                 'user':'qf',      # db user
                 'passwd':'123456',  # db password
                 'charset':'utf8',   # db charset
                 'conn':10}          # db connections in pool
           }

    install(DATABASE)
    install(DATABASE)
    install(DATABASE)
    install(DATABASE)

    with get_connection('test') as conn:
        for i in range(0, 100):
            print conn.select_one('order')



if __name__ == '__main__':
    #test_with()
    #test5()
    #time.sleep(50)
    #pool = dbpool['test']
    #test3()
    #test4()
    #test()
    #test_base_func()
    #test_new_rw()
    test_db_install()
    print 'complete!'



