from .capital_socket import CapitalSocket, memory
from logger import Logger


class CapitalSocketManager:
    def __init__(self):
        self.sockets = []  # List of CapitalSocket instances
        self.max_per_socket = 40

    async def _get_available_socket(self):
        """Find a socket with space, or create a new one."""
        for sock in self.sockets:
            if len(sock.subscribed_epics) < self.max_per_socket:
                return sock

        # No space found — make new socket
        new_socket = CapitalSocket()
        await new_socket.connect_websocket()
        self.sockets.append(new_socket)
        return new_socket

    async def subscribe(self, epic: str):
        """Subscribe to an epic, allocating sockets as needed."""
        sock = await self._get_available_socket()
        await sock.subscribe_to_epic(epic)

    async def unsubscribe(self, epic: str):
        """Unsubscribe from an epic (find the socket that has it)."""
        for sock in self.sockets:
            if epic in sock.subscribed_epics:
                await sock.unsubscribe_from_epic(epic)
                return
        await Logger.app_log(title="UNSUBSCRIBE_ERR", message=f"{epic} not found in any socket")

    async def ping_all(self):
        """Ping all sockets to keep them alive."""
        for sock in self.sockets:
            await sock.ping_socket()

    def get_all_subscribed_epics(self):
        """Get a list of all subscribed epics across all sockets."""
        subscribed_epics = set()
        for sock in self.sockets:
            subscribed_epics.update(sock.subscribed_epics)
        return subscribed_epics


socket_manager = CapitalSocketManager()
