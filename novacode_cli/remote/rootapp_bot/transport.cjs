'use strict';

// SDK calls stay here; Nova owns authorization, session routing and agent execution.
function startRelay(root, enums, config, WebSocket) {
  let socket;
  let stopped = false;
  let reconnect;
  const sent = new Set();
  const pendingBodies = new Set();
  const messages = root.community.channelMessages;
  const send = (body) => {
    if (socket?.readyState === 1) socket.send(JSON.stringify(body));
  };
  const remember = (id) => {
    sent.add(id);
    if (sent.size > 2000) sent.delete(sent.values().next().value);
  };
  const onMessage = (event) => {
    if (event.messageType === enums.MessageType.System || sent.has(event.id)) return;
    const content = event.messageContent || '';
    if (pendingBodies.has(content) || !/^\/nova(?:\s|$)/i.test(content)) return;
    send({
      type: 'message', id: event.id, channel_id: event.channelId,
      user_id: event.userId, text: content.replace(/^\/nova\s*/i, '').trim(),
      parent_ids: (event.parentMessages || []).map(parent => parent.id),
    });
  };
  messages.on(enums.ChannelMessageEvent.ChannelMessageCreated, onMessage);

  async function execute(command) {
    try {
      const request = { channelId: command.channel_id };
      let result;
      if (command.method === 'create') {
        pendingBodies.add(command.content);
        try {
          result = await messages.create({ ...request, content: command.content });
          remember(result.id);
        } finally { pendingBodies.delete(command.content); }
      } else if (command.method === 'edit') {
        result = await messages.edit({ ...request, id: command.message_id, content: command.content });
      } else if (command.method === 'delete') {
        result = await messages.delete({ ...request, id: command.message_id });
      } else if (command.method === 'typing') {
        result = await messages.setTypingIndicator({ ...request, isTyping: true });
      } else {
        throw new Error('Unsupported bridge operation');
      }
      send({ type: 'result', id: command.id, message_id: result?.id || command.message_id || '' });
    } catch (error) {
      // Return a code, never the SDK error object (it can contain auth details).
      const code = String(error?.errorCode || 'sdk_request_failed').slice(0, 100);
      send({ type: 'result', id: command.id, error: code });
    }
  }
  function connect() {
    if (stopped) return;
    socket = new WebSocket(config.url);
    socket.addEventListener('open', () => send({ type: 'auth', secret: config.secret }));
    socket.addEventListener('message', (event) => {
      let body;
      try { body = JSON.parse(String(event.data)); } catch { return; }
      if (body.type === 'connected') send({ type: 'ready' });
      else if (body.type === 'command') void execute(body);
    });
    socket.addEventListener('error', () => {}); // close handles retry, without printing secrets.
    socket.addEventListener('close', () => {
      if (!stopped) reconnect = setTimeout(connect, 1000);
    });
  }
  connect();
  return () => {
    stopped = true;
    clearTimeout(reconnect);
    messages.off(enums.ChannelMessageEvent.ChannelMessageCreated, onMessage);
    socket?.close();
  };
}

module.exports = { startRelay };
