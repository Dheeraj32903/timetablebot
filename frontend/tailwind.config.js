/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        background: {
          DEFAULT: '#080B14',
          secondary: '#0E1322',
          card: '#12182D',
          elevated: '#17203A',
          glass: 'rgba(18, 24, 45, 0.7)',
        },
        brand: {
          50: '#F5F3FF',
          100: '#EDE9FE',
          200: '#DDD6FE',
          300: '#C4B5FD',
          400: '#A78BFA',
          500: '#8B5CF6',
          600: '#7C3AED',
          700: '#6D28D9',
          800: '#5B21B6',
          violet: '#7C3AED',
          indigo: '#6366F1',
          cyan: '#06B6D4',
          emerald: '#10B981',
          rose: '#F43F5E',
          amber: '#F59E0B',
        },
      },
      fontFamily: {
        sans: ['"Rosemary"', '"Plus Jakarta Sans"', 'Inter', '-apple-system', 'sans-serif'],
        display: ['"Rosemary"', 'Outfit', '"Plus Jakarta Sans"', 'sans-serif'],
        rosemary: ['"Rosemary"', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
      },
      boxShadow: {
        'glass': '0 8px 32px 0 rgba(0, 0, 0, 0.37), inset 0 1px 0 0 rgba(255, 255, 255, 0.08)',
        'glass-glow': '0 10px 35px -5px rgba(124, 58, 237, 0.3), inset 0 1px 0 0 rgba(255, 255, 255, 0.15)',
        'neon-violet': '0 0 25px -4px rgba(124, 58, 237, 0.45)',
        'neon-emerald': '0 0 20px -4px rgba(16, 185, 129, 0.4)',
      },
      backgroundImage: {
        'mesh-glow': 'radial-gradient(at 0% 0%, rgba(124, 58, 237, 0.18) 0px, transparent 50%), radial-gradient(at 100% 100%, rgba(99, 102, 241, 0.15) 0px, transparent 50%)',
        'gradient-card': 'linear-gradient(135deg, rgba(28, 22, 60, 0.85) 0%, rgba(14, 19, 36, 0.95) 100%)',
        'gradient-glass': 'linear-gradient(135deg, rgba(255, 255, 255, 0.05) 0%, rgba(255, 255, 255, 0.01) 100%)',
      }
    },
  },
  plugins: [],
}
