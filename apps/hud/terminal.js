import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';

window.CortexTerminal = {
  attach(element, url, onExit = () => {}) {
    const terminal = new Terminal({
      cursorBlink: true,
      convertEol: false,
      scrollback: 5000,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
      fontSize: 12,
      theme: { background: '#090d12', foreground: '#d6e1e8', cursor: '#88f0cc', selectionBackground: '#315e53' },
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(element);
    fit.fit();
    const socket = new WebSocket(url);
    socket.onopen = () => { socket.send(JSON.stringify({ type: 'auth', token: localStorage.getItem('cortexos-token') || '' })); socket.send(JSON.stringify({ type: 'resize', cols: terminal.cols, rows: terminal.rows })); };
    socket.onmessage = event => {
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      if (message.type === 'output') terminal.write(message.data);
      if (message.type === 'exit') { terminal.writeln(`\r\n[process exited with code ${message.code}]`); onExit(message.code); }
    };
    socket.onerror = () => terminal.writeln('\r\n[terminal websocket error]');
    socket.onclose = () => terminal.writeln('\r\n[disconnected from terminal session]');
    terminal.onData(data => { if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'input', data })); });
    terminal.onResize(({ cols, rows }) => { if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'resize', cols, rows })); });
    const observer = new ResizeObserver(() => { fit.fit(); });
    observer.observe(element);
    terminal.focus();
    return {
      terminal,
      interrupt: () => { if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'interrupt' })); },
      disconnect: () => { observer.disconnect(); socket.close(); terminal.dispose(); },
    };
  },
};
