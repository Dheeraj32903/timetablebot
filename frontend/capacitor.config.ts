import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'in.kluniversity.hub',
  appName: 'KL Student Hub',
  webDir: 'dist',
  server: {
    cleartext: true,
    androidScheme: 'https'
  }
};

export default config;
