import React, { useState } from 'react';
import { 
  Settings, 
  Bell, 
  ShieldCheck, 
  KeyRound, 
  User, 
  ExternalLink,
  CheckCircle2,
  Lock,
  Layers,
  Sparkles
} from 'lucide-react';
import { UserProfile, toggleMorningAlerts } from '../api/client';

interface SettingsScreenProps {
  user: UserProfile | null;
  onUserUpdate: (updated: UserProfile) => void;
}

export const SettingsScreen: React.FC<SettingsScreenProps> = ({ user, onUserUpdate }) => {
  const [morningAlerts, setMorningAlerts] = useState<boolean>(user?.morning_alerts_enabled ?? false);
  const [toggling, setToggling] = useState<boolean>(false);

  // Sync state whenever user profile updates
  useEffect(() => {
    if (user?.morning_alerts_enabled !== undefined) {
      setMorningAlerts(user.morning_alerts_enabled);
    }
  }, [user?.morning_alerts_enabled]);

  const handleToggleMorning = async () => {
    if (toggling) return;
    const nextVal = !morningAlerts;
    setMorningAlerts(nextVal); // Optimistic UI toggle
    setToggling(true);
    try {
      const res = await toggleMorningAlerts(nextVal);
      setMorningAlerts(res);
      if (user) {
        onUserUpdate({ ...user, morning_alerts_enabled: res });
      }
      if (window.Telegram?.WebApp?.HapticFeedback) {
        window.Telegram.WebApp.HapticFeedback.impactOccurred('medium');
      }
    } catch (err: any) {
      console.error('Failed to toggle morning alerts:', err);
      setMorningAlerts(!nextVal); // Rollback on failure
      const errorMsg = err?.response?.data?.detail || err?.message || 'Failed to update setting';
      alert(`Could not toggle morning alert: ${errorMsg}`);
    } finally {
      setToggling(false);
    }
  };

  return (
    <div className="p-4 space-y-4 max-w-md mx-auto pb-28">
      {/* Header */}
      <div className="flex items-center gap-2">
        <Settings size={20} className="text-brand-400" />
        <h2 className="text-lg font-bold text-white tracking-tight">Settings & Preferences</h2>
      </div>

      {/* Account Profile Card */}
      <div className="p-4 rounded-2xl bg-[#111625] border border-white/5 space-y-3">
        <div className="flex items-center gap-3">
          <div className="w-11 h-11 rounded-xl bg-gradient-to-tr from-brand-violet to-brand-500 flex items-center justify-center text-white font-bold text-sm shadow-glow-violet">
            {user?.first_name ? user.first_name[0] : 'S'}
          </div>
          <div>
            <div className="text-sm font-semibold text-white">
              {user ? `${user.first_name} ${user.last_name}` : 'Student User'}
            </div>
            <div className="text-xs text-slate-400 font-mono">
              Student ID: {user?.student_id || 'Not Linked'}
            </div>
            {user?.telegram_username && (
              <div className="text-[11px] text-brand-300">
                @{user.telegram_username}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Integrations Status */}
      <div className="space-y-2">
        <div className="text-xs font-semibold text-slate-300 px-1">
          System Integrations
        </div>

        <div className="p-3.5 rounded-2xl bg-[#111625] border border-white/5 space-y-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2.5">
              <div className="w-8 h-8 rounded-lg bg-white/5 flex items-center justify-center text-slate-300">
                <Layers size={16} />
              </div>
              <div>
                <div className="text-xs font-semibold text-white">KL University ERP</div>
                <div className="text-[10px] text-slate-400">Timetable & Hostel Gate Pass</div>
              </div>
            </div>
            <span
              className={`text-[10px] font-medium px-2 py-0.5 rounded-full flex items-center gap-1 ${
                user?.erp.connected
                  ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'
                  : 'bg-white/5 text-slate-400 border border-white/10'
              }`}
            >
              <span className={`w-1.5 h-1.5 rounded-full ${user?.erp.connected ? 'bg-emerald-400' : 'bg-slate-500'}`} />
              <span>{user?.erp.status_text || 'Not Linked'}</span>
            </span>
          </div>

          <div className="border-t border-white/5 pt-3 flex items-center justify-between">
            <div className="flex items-center gap-2.5">
              <div className="w-8 h-8 rounded-lg bg-white/5 flex items-center justify-center text-slate-300">
                <Sparkles size={16} />
              </div>
              <div>
                <div className="text-xs font-semibold text-white">Moodle LMS Portal</div>
                <div className="text-[10px] text-slate-400">Assignments, Quizzes & Deadlines</div>
              </div>
            </div>
            <span
              className={`text-[10px] font-medium px-2 py-0.5 rounded-full flex items-center gap-1 ${
                user?.lms.connected
                  ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'
                  : 'bg-white/5 text-slate-400 border border-white/10'
              }`}
            >
              <span className={`w-1.5 h-1.5 rounded-full ${user?.lms.connected ? 'bg-emerald-400' : 'bg-slate-500'}`} />
              <span>{user?.lms.status_text || 'Not Linked'}</span>
            </span>
          </div>
        </div>
      </div>

      {/* Preferences & Notifications */}
      <div className="space-y-2">
        <div className="text-xs font-semibold text-slate-300 px-1">
          Automated Dispatches
        </div>

        <div className="p-3.5 rounded-2xl bg-[#111625] border border-white/5 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-white/5 flex items-center justify-center text-slate-300">
              <Bell size={16} />
            </div>
            <div>
              <div className="text-xs font-semibold text-white">Morning Timetable Alert</div>
              <div className="text-[10px] text-slate-400">Automated daily summary at 06:45 AM IST</div>
            </div>
          </div>

          <button
            onClick={handleToggleMorning}
            disabled={toggling}
            className={`w-11 h-6 flex items-center rounded-full p-0.5 transition-colors ${
              morningAlerts ? 'bg-brand-violet' : 'bg-slate-700'
            }`}
          >
            <div
              className={`bg-white w-5 h-5 rounded-full shadow-md transform transition-transform ${
                morningAlerts ? 'translate-x-5' : 'translate-x-0'
              }`}
            />
          </button>
        </div>
      </div>
    </div>
  );
};

