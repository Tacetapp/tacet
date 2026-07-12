'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// Settings API — consumed by the settings window (settings.html / settings.js).
// schema() is async (fetches over IPC) so the sandbox can stay on; previously
// this required('./config') which broke sandboxed preloads (§5.4).
contextBridge.exposeInMainWorld('settingsAPI', {
  schema: () => ipcRenderer.invoke('settings:schema'),
  get: () => ipcRenderer.invoke('settings:get'),
  save: (config) => ipcRenderer.invoke('settings:save', config),
  pickDirectory: () => ipcRenderer.invoke('settings:pickDirectory'),
  openLog: () => ipcRenderer.invoke('settings:openLog'),
  // Returns array of password-field keys that have a stored secret, so the UI
  // can show a "(stored securely)" placeholder (§4.4).
  secretsPresent: () => ipcRenderer.invoke('settings:secretsPresent'),
});

// Desktop bridge — consumed by the main meeting UI (served by Python backend).
// Lets the renderer push meeting state to the main process (for tray + power),
// and receive commands from the tray/global-shortcut (§5.3 item 1 & 2).
contextBridge.exposeInMainWorld('desktop', {
  // Push { active, paused, elapsed } to the tray + powerSaveBlocker.
  setMeetingState: (meetingState) => ipcRenderer.send('meeting:state', meetingState),
  // Register a one-time callback for tray/shortcut → renderer commands:
  // 'start' | 'pause' | 'resume' | 'stop'
  onMeetingCommand: (cb) => {
    ipcRenderer.on('meeting:command', (_evt, cmd) => cb(cmd));
  },
});
