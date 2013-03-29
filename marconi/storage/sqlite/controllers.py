# Copyright (c) 2013 Rackspace, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from marconi.storage import base
from marconi.storage import exceptions


class Queue(base.QueueBase):
    def __init__(self, driver):
        self.driver = driver
        self.driver.run('''
            create table
            if not exists
            Queues (
                id INTEGER,
                tenant TEXT,
                name TEXT,
                metadata DOCUMENT,
                PRIMARY KEY(id),
                UNIQUE(tenant, name)
            )
        ''')

    def list(self, tenant):
        records = self.driver.run('''
            select name, metadata from Queues
             where tenant = ?''', tenant)

        for k, v in records:
            yield {'name': k, 'metadata': v}

    def get(self, name, tenant):
        try:
            return self.driver.get('''
                select metadata from Queues
                 where tenant = ? and name = ?''', tenant, name)[0]

        except _NoResult:
            _queue_doesnotexist(name, tenant)

    def upsert(self, name, metadata, tenant):
        with self.driver('immediate'):
            previous_record = self.driver.run('''
                select id from Queues
                 where tenant = ? and name = ?
            ''', tenant, name).fetchone()

            self.driver.run('''
                replace into Queues
                 values (null, ?, ?, ?)
            ''', tenant, name, self.driver.pack(metadata))

            return previous_record is None

    def delete(self, name, tenant):
        self.driver.run('''
            delete from Queues
             where tenant = ? and name = ?''', tenant, name)

    def stats(self, name, tenant):
        qid, messages = self.driver.get('''
            select Q.id, count(M.id)
              from Queues as Q join Messages as M
                on qid = Q.id
             where tenant = ? and name = ?''', tenant, name)

        if qid is None:
            _queue_doesnotexist(name, tenant)

        return {
            'messages': messages,
            'actions': 0,
        }

    def actions(self, name, tenant, marker=None, limit=10):
        pass


class Message(base.MessageBase):
    def __init__(self, driver):
        self.driver = driver
        self.driver.run('''
            create table
            if not exists
            Messages (
                id INTEGER,
                qid INTEGER,
                ttl INTEGER,
                content DOCUMENT,
                client TEXT,
                created DATETIME,  -- seconds since the Julian day
                PRIMARY KEY(id),
                FOREIGN KEY(qid) references Queues(id) on delete cascade
            )
        ''')

    def get(self, queue, message_id, tenant):
        try:
            content, ttl, age = self.driver.get('''
                select content, ttl, julianday() * 86400.0 - created
                  from Queues as Q join Messages as M
                    on qid = Q.id
                 where ttl > julianday() * 86400.0 - created
                   and M.id = ? and tenant = ? and name = ?
            ''', _msgid_decode(message_id), tenant, queue)

            return {
                'id': message_id,
                'ttl': ttl,
                'age': int(age),
                'body': content,
            }

        except (_NoResult, _BadID):
            _msg_doesnotexist(message_id)

    def list(self, queue, tenant, marker=None,
             limit=10, echo=False, client_uuid=None):
        with self.driver('deferred'):
            try:
                sql = '''
                    select id, content, ttl, julianday() * 86400.0 - created
                      from Messages
                     where ttl > julianday() * 86400.0 - created
                       and qid = ?'''
                args = [_get_qid(self.driver, queue, tenant)]

                if not echo:
                    sql += '''
                       and client != ?'''
                    args += [client_uuid]

                if marker:
                    sql += '''
                       and id > ?'''
                    args += [_marker_decode(marker)]

                sql += '''
                     limit ?'''
                args += [limit]

                iter = self.driver.run(sql, *args)

                for id, content, ttl, age in iter:
                    yield {
                        'id': _msgid_encode(id),
                        'ttl': ttl,
                        'age': int(age),
                        'marker': _marker_encode(id),
                        'body': content,
                    }

            except _BadID:
                return

    def post(self, queue, messages, tenant, client_uuid):
        with self.driver('immediate'):
            qid = _get_qid(self.driver, queue, tenant)

            # cleanup all expired messages in this queue

            self.driver.run('''
                delete from Messages
                where ttl <= julianday() * 86400.0 - created
                  and qid = ?''', qid)

            # executemany() sets lastrowid to None, so no matter we manually
            # generate the IDs or not, we still need to query for it.

            unused = self.driver.get('''
                select max(id) + 1 from Messages''')[0] or 1001

            def it(newid):
                for m in messages:
                    yield (newid, qid, m['ttl'],
                           self.driver.pack(m['body']), client_uuid)
                    newid += 1

            self.driver.run_multiple('''
                insert into Messages
                values (?, ?, ?, ?, ?, julianday() * 86400.0)''', it(unused))

        return map(_msgid_encode, range(unused, unused + len(messages)))

    def delete(self, queue, message_id, tenant, claim=None):
        try:
            self.driver.run('''
                delete from Messages
                 where id = ?
                   and qid = (select id from Queues
                               where tenant = ? and name = ?)
            ''', _msgid_decode(message_id), tenant, queue)

        except _BadID:
            pass


class _NoResult(Exception):
    pass


class _BadID(Exception):
    pass


def _queue_doesnotexist(name, tenant):
    msg = (_("Queue %(name)s does not exist for tenant %(tenant)s")
           % dict(name=name, tenant=tenant))

    raise exceptions.DoesNotExist(msg)


def _msg_doesnotexist(id):
    msg = (_("Message %(id)s does not exist")
           % dict(id=id))

    raise exceptions.DoesNotExist(msg)


def _get_qid(driver, queue, tenant):
    try:
        return driver.get('''
            select id from Queues
             where tenant = ? and name = ?''', tenant, queue)[0]

    except _NoResult:
        _queue_doesnotexist(queue, tenant)


# The utilities below make the database IDs opaque to the users
# of Marconi API.  The only purpose is to advise the users NOT to
# make assumptions on the implementation of and/or relationship
# between the message IDs, the markers, and claim IDs.
#
# The magic numbers are arbitrarily picked; the numbers themselves
# come with no special functionalities.

def _msgid_encode(id):
    return hex(id ^ 0x5c693a53)[2:]


def _msgid_decode(id):
    try:
        return int(id, 16) ^ 0x5c693a53

    except ValueError:
        raise _BadID


def _marker_encode(id):
    return oct(id ^ 0x3c96a355)[1:]


def _marker_decode(id):
    try:
        return int(id, 8) ^ 0x3c96a355

    except ValueError:
        raise _BadID