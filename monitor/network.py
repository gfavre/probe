"""Per-session IP family selection; no global DNS monkey patching."""
import socket
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError
from urllib3.poolmanager import PoolManager


def connect(host, port, timeout, family=socket.AF_UNSPEC, socket_options=()):
    last_error = OSError('Aucune adresse disponible')
    for af, kind, proto, _, address in socket.getaddrinfo(host, port, family, socket.SOCK_STREAM):
        sock = socket.socket(af, kind, proto)
        try:
            sock.settimeout(timeout)
            for option in socket_options or ():
                sock.setsockopt(*option)
            sock.connect(address)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise last_error


class FamilyAdapter(HTTPAdapter):
    def __init__(self, family):
        self.family = family
        super().__init__(max_retries=0)

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        family = self.family

        class ConnectionMixin:
            def _new_conn(self):
                try:
                    return connect(self._dns_host, self.port, self.timeout, family, self.socket_options)
                except OSError as exc:
                    raise NewConnectionError(self, 'Connexion IP impossible') from exc

        class HTTPFamily(ConnectionMixin, HTTPConnection):
            pass

        class HTTPSFamily(ConnectionMixin, HTTPSConnection):
            pass

        class HTTPPool(HTTPConnectionPool):
            ConnectionCls = HTTPFamily

        class HTTPSPool(HTTPSConnectionPool):
            ConnectionCls = HTTPSFamily

        self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize, block=block, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {'http': HTTPPool, 'https': HTTPSPool}
