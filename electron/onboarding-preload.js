'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// Exposed to the onboarding window only.
contextBridge.exposeInMainWorld('onboardingAPI', {
  status: () => ipcRenderer.invoke('onboarding:status'),
  requestMic: () => ipcRenderer.invoke('onboarding:requestMic'),
  openScreenSettings: () => ipcRenderer.invoke('onboarding:openScreenSettings'),
  complete: (data) => ipcRenderer.invoke('onboarding:complete', data),
});
