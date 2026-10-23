// Root's DevHost loads this CommonJS entry point, just like compiled starter code.
const { rootServer, ChannelMessageEvent, MessageType } = require('@rootsdk/server-bot');
const WebSocket = require('ws');
const { startRelay } = require('./transport.cjs');
const config = require('./connection.json');

// DevHost forks this process. Windows termination of the host does not signal
// its children, but closing the IPC channel reliably tells us to exit too.
process.on('disconnect', () => process.exit(0));

rootServer.lifecycle.start(async () => {
  startRelay(rootServer, { ChannelMessageEvent, MessageType }, config, WebSocket);
}).catch(() => {
  console.error('Rootapp lifecycle failed. Check the Dev Token, Bot ID and community permissions.');
  process.exitCode = 1;
});
