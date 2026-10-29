import React from 'react';
import { RefreshCw, Sparkles, ShieldCheck } from 'lucide-react';
import { UserProfile } from '../api/client';

interface HeaderProps {
  user: UserProfile | null;
  onRefresh?: () => void;
  isRefreshing?: boolean;
}

export const Header: React.FC<HeaderProps> = ({ user, onRefresh, isRefreshing = false }) => {
  const triggerHaptic = () => {
    if (window.Telegram?.WebApp?.HapticFeedback) {
      window.Telegram.WebApp.HapticFeedback.impactOccurred('light');
    } else if (navigator?.vibrate) {
      navigator.vibrate(10);
    }
  };

  const handleRefreshClick = () => {
    triggerHaptic();
    onRefresh?.();
  };

  return (
    <header className="sticky top-0 z-40 bg-[#080B14] border-b border-white/5 px-4 py-3">
      <div className="flex items-center justify-between">
        {/* Left: Avatar & Identity matching previous exact UI */}
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-[#7C3AED] flex items-center justify-center font-display font-bold text-white text-sm shadow-sm">
            {user?.first_name ? user.first_name.slice(0, 2).toUpperCase() : 'KL'}
          </div>

          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-base font-bold text-white tracking-tight">
                {user?.first_name || 'Dheeraj'}
              </h1>
              <span className="text-xs font-mono bg-[#1E1B4B] text-[#A5B4FC] border border-[#4338CA]/40 px-2 py-0.5 rounded-md font-medium">
                {user?.student_id || '2400032903'}
              </span>
            </div>

            <div className="flex items-center gap-2 text-[11px] text-slate-400 font-sans mt-0.5">
              <span className="flex items-center gap-1.5">
                <span className={`w-1.5 h-1.5 rounded-full ${user?.erp.connected ? 'bg-emerald-400' : 'bg-slate-500'}`} />
                <span className="font-medium text-slate-300">ERP</span>
              </span>
              <span className="text-slate-600">•</span>
              <span className="flex items-center gap-1.5">
                <span className={`w-1.5 h-1.5 rounded-full ${user?.lms.connected ? 'bg-emerald-400' : 'bg-slate-500'}`} />
                <span className="font-medium text-slate-300">LMS</span>
              </span>
            </div>
          </div>
        </div>

        {/* Right: Actions matching previous exact UI */}
        <div className="flex items-center gap-2">
          {onRefresh && (
            <button
              onClick={handleRefreshClick}
              disabled={isRefreshing}
              className="w-9 h-9 rounded-xl bg-white/5 hover:bg-white/10 active:scale-95 border border-white/10 flex items-center justify-center text-slate-300 hover:text-white transition-all shadow-sm"
              title="Sync All Services"
            >
              <RefreshCw
                size={16}
                className={`transition-transform ${isRefreshing ? 'animate-spin text-brand-400' : ''}`}
              />
            </button>
          )}
        </div>
      </div>
    </header>
  );
};
