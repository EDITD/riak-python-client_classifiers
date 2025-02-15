# Copyright 2010-present Basho Technologies, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import select
import socket

from riak.security import SecurityError, USE_STDLIB_SSL
from riak.transports.http.transport import HttpTransport
from riak.transports.pool import Pool

if USE_STDLIB_SSL:
    import ssl
    from riak.transports.security import configure_ssl_context
else:
    import OpenSSL.SSL
    from riak.transports.security import RiakWrappedSocket,\
        configure_pyopenssl_context

from http.client import HTTPConnection, \
    HTTPSConnection, \
    NotConnected, \
    IncompleteRead, \
    ImproperConnectionState, \
    BadStatusLine


class NoNagleHTTPConnection(HTTPConnection):
    """
    Setup a connection class which does not use Nagle - deal with
    latency on PUT requests lower than MTU
    """
    def connect(self):
        """
        Set TCP_NODELAY on socket
        """
        HTTPConnection.connect(self)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


# Inspired by
# http://code.activestate.com/recipes/577548-https-httplib-client-connection-with-certificate-v/
class RiakHTTPSConnection(HTTPSConnection):
    def __init__(self,
                 host,
                 port,
                 credentials,
                 pkey_file=None,
                 cert_file=None,
                 timeout=None):
        """
        Class to make a HTTPS connection,
        with support for full client-based SSL Authentication

        :param host: Riak host name
        :type host: str
        :param port: Riak host port number
        :type port: int
        :param credentials: Security Credential settings
        :type  credentials: SecurityCreds
        :param pkey_file: PEM formatted file that contains your private key
        :type pkey_file: str
        :param cert_file: PEM formatted certificate chain file
        :type cert_file: str
        :param timeout: Number of seconds before timing out
        :type timeout: int
        """
        super(RiakHTTPSConnection, self). \
            __init__(host=host,
                     port=port,
                     key_file=credentials._pkey_file,
                     cert_file=credentials._cert_file)
        self.pkey_file = pkey_file
        self.cert_file = cert_file
        self.credentials = credentials
        self.timeout = timeout

    def connect(self):
        """
        Connect to a host on a given (SSL) port using PyOpenSSL.
        """
        sock = socket.create_connection((self.host, self.port), self.timeout)
        if not USE_STDLIB_SSL:
            ssl_ctx = configure_pyopenssl_context(self.credentials)

            # attempt to upgrade the socket to TLS
            cxn = OpenSSL.SSL.Connection(ssl_ctx, sock)
            cxn.set_connect_state()
            while True:
                try:
                    cxn.do_handshake()
                except OpenSSL.SSL.WantReadError:
                    select.select([sock], [], [])
                    continue
                except OpenSSL.SSL.Error as e:
                    raise SecurityError(f"bad handshake - {str(e)}")
                break

            self.sock = RiakWrappedSocket(cxn, sock)
            self.credentials._check_revoked_cert(self.sock)
        else:
            ssl_ctx = configure_ssl_context(self.credentials)
            if self.timeout is not None:
                sock.settimeout(self.timeout)
            self.sock = ssl.SSLSocket(sock=sock,
                                      keyfile=self.credentials.pkey_file,
                                      certfile=self.credentials.cert_file,
                                      cert_reqs=ssl.CERT_REQUIRED,
                                      ca_certs=self.credentials.cacert_file,
                                      ciphers=self.credentials.ciphers,
                                      server_hostname=self.host)
            self.sock.context = ssl_ctx


class HttpPool(Pool):
    """
    A pool of HTTP(S) transport connections.
    """
    def __init__(self, client, **options):
        self.client = client
        self.options = options
        self.connection_class = NoNagleHTTPConnection
        if self.client._credentials:
            self.connection_class = RiakHTTPSConnection

        super(HttpPool, self).__init__()

    def create_resource(self):
        node = self.client._choose_node()
        return HttpTransport(node=node,
                             client=self.client,
                             connection_class=self.connection_class,
                             **self.options)

    def destroy_resource(self, transport):
        transport.close()


CONN_CLOSED_ERRORS = (
    NotConnected,
    IncompleteRead,
    ImproperConnectionState,
    BadStatusLine,
)


def is_retryable(err):
    """
    Determines if the given exception is something that is
    network/socket-related and should thus cause the HTTP connection
    to close and the operation retried on another node.

    :rtype: boolean
    """
    for errtype in CONN_CLOSED_ERRORS:
        if isinstance(err, errtype):
            return True
    return False
