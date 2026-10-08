import { App, Modal, Notice, Plugin, PluginSettingTab, Setting, requestUrl } from 'obsidian';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';

interface CortexSettings { bridgeUrl: string; backend: string }
const DEFAULTS: CortexSettings = { bridgeUrl: 'http://127.0.0.1:8765', backend: 'codex' };

class CortexPromptModal extends Modal {
  plugin: CortexPlugin;
  input!: HTMLTextAreaElement;
  recordButton!: HTMLButtonElement;
  resultEl!: HTMLElement;
  recorder: MediaRecorder | null = null;
  stream: MediaStream | null = null;
  chunks: BlobPart[] = [];
  speakAfterApproval = false;
  terminal: Terminal | null = null;
  terminalSocket: WebSocket | null = null;
  terminalSessionId: string | null = null;
  terminalFit: FitAddon | null = null;
  constructor(app: App, plugin: CortexPlugin) { super(app); this.plugin = plugin; }
  onOpen() {
    const { contentEl } = this; contentEl.createEl('h2', { text: 'CortexOS' });
    this.input = contentEl.createEl('textarea', { attr: { rows: '5', placeholder: 'What would you like CortexOS to do?' } });
    this.recordButton = contentEl.createEl('button', { text: 'Record voice command (local Whisper)' });
    this.recordButton.onclick = () => this.toggleRecording();
    const button = contentEl.createEl('button', { text: 'Run request' });
    button.onclick = () => this.runText(button);
    const terminalButton = contentEl.createEl('button', { text: 'Open interactive agent terminal' });
    const terminalBox = contentEl.createDiv({ cls: 'cortex-terminal-box' });
    terminalBox.style.cssText = 'height:280px;display:none;margin-top:12px;padding:8px;background:#090d12';
    terminalButton.onclick = () => this.openTerminal(terminalBox, terminalButton);
    this.resultEl = contentEl.createDiv({ cls: 'cortex-response' });
  }
  async openTerminal(container: HTMLElement, button: HTMLButtonElement) {
    if (this.terminalSocket) { this.terminal?.focus(); return; }
    button.disabled = true;
    try {
      const response = await requestUrl({ url: `${this.plugin.settings.bridgeUrl}/api/sessions`, method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ backend: this.plugin.settings.backend, cols: 100, rows: 24 }) });
      const data = response.json; this.terminalSessionId = data.id; container.style.display = 'block';
      this.terminal = new Terminal({ cursorBlink: true, scrollback: 3000, fontSize: 12, theme: { background: '#090d12', foreground: '#d6e1e8', cursor: '#88f0cc' } });
      this.terminalFit = new FitAddon(); this.terminal.loadAddon(this.terminalFit); this.terminal.open(container); this.terminalFit.fit();
      const wsUrl = `${this.plugin.settings.bridgeUrl.replace(/^http/, 'ws')}${data.websocket}`;
      this.terminalSocket = new WebSocket(wsUrl);
      this.terminalSocket.onopen = () => this.terminalSocket?.send(JSON.stringify({ type: 'resize', cols: this.terminal?.cols || 100, rows: this.terminal?.rows || 24 }));
      this.terminalSocket.onmessage = event => { const message = JSON.parse(event.data); if (message.type === 'output') this.terminal?.write(message.data); else if (message.type === 'exit') this.terminal?.writeln(`\r\n[process exited with code ${message.code}]`); };
      this.terminal.onData(data => { if (this.terminalSocket?.readyState === WebSocket.OPEN) this.terminalSocket.send(JSON.stringify({ type: 'input', data })); });
      this.terminal.onResize(({ cols, rows }) => { if (this.terminalSocket?.readyState === WebSocket.OPEN) this.terminalSocket.send(JSON.stringify({ type: 'resize', cols, rows })); });
      new Notice(`${data.backend} terminal connected. Type in the terminal pane; Ctrl+C interrupts.`);
      button.setText('Terminal open · click to focus'); button.disabled = false;
      button.onclick = () => this.terminal?.focus();
    } catch (err) { new Notice(`Could not open terminal: ${String(err)}`); button.disabled = false; }
  }
  async toggleRecording() {
    if (this.recorder?.state === 'recording') { this.recorder.stop(); this.recordButton.setText('Transcribing locally…'); return; }
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: true }); this.chunks = [];
      this.recorder = new MediaRecorder(this.stream);
      this.recorder.ondataavailable = event => { if (event.data.size) this.chunks.push(event.data); };
      this.recorder.onstop = () => {
        const blob = new Blob(this.chunks, { type: this.recorder?.mimeType || 'audio/webm' });
        this.stream?.getTracks().forEach(track => track.stop()); this.stream = null;
        if (blob.size > 20 * 1024 * 1024) { this.showError('Recording is larger than the 20 MB limit.'); return; }
        void this.runVoice(blob);
      };
      this.recorder.start(); this.recordButton.setText('Stop recording');
    } catch (err) { this.showError(`Could not access the microphone: ${String(err)}`); this.offerBrowserStt(); }
  }
  async runVoice(blob: Blob) {
    try {
      const bytes = new Uint8Array(await blob.arrayBuffer()); let binary = '';
      for (let offset = 0; offset < bytes.length; offset += 0x8000) binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
      const response = await requestUrl({ url: `${this.plugin.settings.bridgeUrl}/api/voice/turn`, method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ audio_base64: btoa(binary), content_type: blob.type || 'audio/webm', backend: this.plugin.settings.backend, background: true, request_id: crypto.randomUUID() }) });
      this.recordButton.setText('Record voice command (local Whisper)');
      const data = response.json;
      this.input.value = data.transcript || '';
      if (data.status === 'running') { this.watchRequest(data.id, true); return; }
      if (data.status === 'awaiting_approval') { this.speakAfterApproval = true; this.showApproval(data); await this.speak('This request needs your approval before it can continue.'); return; }
      await this.showResult(data, true);
    } catch (err) {
      this.recordButton.setText('Record voice command (local Whisper)');
      this.showError(`Local Whisper failed: ${String(err)}. The recording stays on this device unless you choose browser fallback.`);
      this.offerBrowserStt();
    }
  }
  offerBrowserStt() {
    const fallback = this.resultEl.createEl('button', { text: 'Last resort: browser speech recognition (may use an online service)' });
    fallback.onclick = () => {
      const Recognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
      if (!Recognition) { this.showError('This Obsidian environment does not provide browser speech recognition.'); return; }
      const recognition = new Recognition(); recognition.lang = 'en-US';
      recognition.onresult = (event: any) => { this.input.value = event.results[0][0].transcript; void this.runText(); };
      recognition.onerror = (event: any) => this.showError(`Browser speech fallback failed: ${event.error}`);
      recognition.start(); new Notice('Browser fallback enabled. Audio may be processed by a browser or OS service.');
    };
  }
  async runText(button?: HTMLButtonElement) {
    const prompt = this.input.value.trim(); if (!prompt) return;
    if (button) { button.disabled = true; button.setText('Working…'); }
    try {
      const response = await requestUrl({ url: `${this.plugin.settings.bridgeUrl}/api/requests`, method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt, backend: this.plugin.settings.backend, background: true, request_id: crypto.randomUUID() }) });
      const data = response.json;
      if (data.status === 'running') { this.watchRequest(data.id, false); return; }
      if (data.status === 'awaiting_approval') { this.showApproval(data); return; }
      await this.showResult(data, false);
    } catch (err) { this.showError(`CortexOS Bridge error: ${String(err)}`); }
    finally { if (button) { button.disabled = false; button.setText('Run request'); } }
  }
  watchRequest(requestId: string, speakOutLoud: boolean) {
    this.resultEl.empty(); this.resultEl.createEl('p', { text: 'Tier 3 agent running. Live output:' });
    const output = this.resultEl.createEl('pre'); output.style.cssText = 'height:180px;overflow:auto;white-space:pre-wrap;background:#090d12;padding:10px';
    const interrupt = this.resultEl.createEl('button', { text: 'Interrupt skill run' });
    const wsUrl = `${this.plugin.settings.bridgeUrl.replace(/^http/, 'ws')}/api/requests/${requestId}/stream`;
    const socket = new WebSocket(wsUrl);
    interrupt.onclick = () => { if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'interrupt' })); };
    socket.onmessage = event => {
      const message = JSON.parse(event.data);
      if (message.type === 'output') { output.textContent += message.data; output.scrollTop = output.scrollHeight; }
      else if (message.type === 'notice') output.textContent += `\n${message.text}\n`;
      else if (message.type === 'done') { socket.close(); void this.showResult(message.result || { error: 'Request ended without a result' }, speakOutLoud); }
    };
    socket.onerror = () => { output.textContent += '\n[Could not connect to the live output stream.]'; };
  }
  showApproval(data: any) {
    this.resultEl.empty(); this.resultEl.createEl('p', { text: 'This request may send, spend, or publish. Approve explicitly before CortexOS continues.' });
    const approve = this.resultEl.createEl('button', { text: 'Approve and run' });
    approve.onclick = async () => {
      approve.disabled = true; approve.setText('Working…');
      try { const response = await requestUrl({ url: `${this.plugin.settings.bridgeUrl}/api/requests/${data.id}/approve?background=true`, method: 'POST' }); const result = response.json; if (result.status === 'running') this.watchRequest(data.id, this.speakAfterApproval); else await this.showResult(result, this.speakAfterApproval); }
      catch (err) { this.showError(`Approval request failed: ${String(err)}`); }
    };
  }
  async showResult(data: any, speakOutLoud: boolean) {
    if (data.status === 'awaiting_approval') { this.speakAfterApproval = speakOutLoud; this.showApproval(data); if (speakOutLoud) await this.speak('This request needs your approval before it can continue.'); return; }
    this.resultEl.empty(); this.resultEl.createEl('pre', { text: data.result || data.error || 'No response' });
    const speakButton = this.resultEl.createEl('button', { text: 'Speak with local Kokoro' });
    speakButton.onclick = () => this.speak(data.result || data.error || 'No response');
    if (speakOutLoud && data.result) await this.speak(data.result);
    const folder = this.app.vault.getAbstractFileByPath('output/CortexOS');
    if (!folder) await this.app.vault.createFolder('output/CortexOS').catch(() => {});
    const file = `output/CortexOS/${Date.now()}-response.md`;
    await this.app.vault.create(file, `# CortexOS response\n\n**Request:** ${this.input.value}\n\n${data.result || data.error || ''}\n`);
    new Notice(`CortexOS response saved to ${file}`);
  }
  async speak(text: string) {
    try {
      const response = await requestUrl({ url: `${this.plugin.settings.bridgeUrl}/api/voice/speak`, method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: text.slice(0, 5000) }) });
      const audio = new Audio(URL.createObjectURL(new Blob([response.arrayBuffer], { type: 'audio/wav' })));
      await audio.play();
    } catch (err) {
      this.showError(`Local Kokoro failed: ${String(err)}`);
      const fallback = this.resultEl.createEl('button', { text: 'Last resort: browser/system speech (may use an online service)' });
      fallback.onclick = () => { const utterance = new SpeechSynthesisUtterance(text); window.speechSynthesis.speak(utterance); };
    }
  }
  showError(message: string) { this.resultEl.createEl('p', { text: message, cls: 'cortex-error' }); }
  onClose() {
    this.stream?.getTracks().forEach(track => track.stop());
    this.terminalSocket?.close(); this.terminal?.dispose();
    if (this.terminalSessionId) void requestUrl({ url: `${this.plugin.settings.bridgeUrl}/api/sessions/${this.terminalSessionId}`, method: 'DELETE' }).catch(() => {});
    this.contentEl.empty();
  }
}

class CortexAutomationModal extends Modal {
  plugin: CortexPlugin;
  constructor(app: App, plugin: CortexPlugin) { super(app); this.plugin = plugin; }
  async onOpen() {
    const { contentEl } = this; contentEl.createEl('h2', { text: 'Skill automation review' });
    contentEl.createEl('p', { text: 'Five user-confirmed successful runs make a skill eligible for review. Approval does not remove per-run gates for sending, spending, or publishing.' });
    try {
      const response = await requestUrl({ url: `${this.plugin.settings.bridgeUrl}/api/skills` });
      for (const skill of response.json as any[]) {
        const row = contentEl.createDiv(); row.style.cssText = 'padding:10px 0;border-bottom:1px solid var(--background-modifier-border)';
        row.createEl('strong', { text: skill.name });
        row.createEl('p', { text: `${skill.successful_runs || 0}/5 confirmed successful runs · ${skill.approval_required ? 'Per-run approval required' : 'No external-action gate'} · promotion: ${skill.promotion_status}` });
        const action = row.createEl('button', { text: skill.promotion_status === 'pending_review' ? 'Approve promotion' : skill.promotion_status === 'approved' ? 'Approved' : 'Promote to automation?' });
        action.disabled = skill.promotion_status === 'approved' || (skill.promotion_status !== 'pending_review' && (skill.successful_runs || 0) < 5);
        action.onclick = async () => {
          action.disabled = true;
          try {
            const endpoint = skill.promotion_status === 'pending_review' ? 'promote/approve' : 'promote';
            await requestUrl({ url: `${this.plugin.settings.bridgeUrl}/api/skills/${encodeURIComponent(skill.id)}/${endpoint}`, method: 'POST' });
            new Notice(skill.promotion_status === 'pending_review' ? 'Automation promotion approved.' : 'Promotion review recorded.');
            this.close(); new CortexAutomationModal(this.app, this.plugin).open();
          } catch (err) { new Notice(`Promotion review failed: ${String(err)}`); action.disabled = false; }
        };
      }
    } catch (err) { contentEl.createEl('p', { text: `Bridge unavailable: ${String(err)}` }); }
  }
  onClose() { this.contentEl.empty(); }
}

class CortexPlugin extends Plugin {
  settings: CortexSettings = DEFAULTS;
  async onload() {
    this.settings = Object.assign({}, DEFAULTS, await this.loadData());
    this.addCommand({ id: 'open-command', name: 'Open CortexOS command', callback: () => new CortexPromptModal(this.app, this).open() });
    this.addCommand({ id: 'automation-review', name: 'Review skill automation promotions', callback: () => new CortexAutomationModal(this.app, this).open() });
    this.addRibbonIcon('brain', 'CortexOS', () => new CortexPromptModal(this.app, this).open());
    this.addSettingTab(new CortexSettingsTab(this.app, this));
  }
  async saveSettings() { await this.saveData(this.settings); }
}

class CortexSettingsTab extends PluginSettingTab {
  plugin: CortexPlugin;
  constructor(app: App, plugin: CortexPlugin) { super(app, plugin); this.plugin = plugin; }
  display() {
    const { containerEl } = this; containerEl.empty(); containerEl.createEl('h2', { text: 'CortexOS Bridge' });
    new Setting(containerEl).setName('Bridge URL').setDesc('Keep the Bridge bound to localhost.').addText(text => text.setValue(this.plugin.settings.bridgeUrl).onChange(async value => { this.plugin.settings.bridgeUrl = value; await this.plugin.saveSettings(); }));
    new Setting(containerEl).setName('Backend').addDropdown(drop => drop.addOption('codex', 'Codex').addOption('claude', 'Claude Code').setValue(this.plugin.settings.backend).onChange(async value => { this.plugin.settings.backend = value; await this.plugin.saveSettings(); }));
  }
}

export default CortexPlugin;
