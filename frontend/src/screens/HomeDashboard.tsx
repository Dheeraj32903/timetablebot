import React, { useState, useEffect } from 'react';
import { 
  GraduationCap, 
  CheckCircle2, 
  Calendar, 
  CheckSquare, 
  BookOpen, 
  Bell, 
  Clock, 
  MapPin, 
  ChevronRight,
  Wifi,
  WifiOff,
  BellRing,
  ShieldCheck,
  Zap
} from 'lucide-react';
import { DashboardResponse, toggleMorningAlerts } from '../api/client';
import { TabType } from '../components/BottomNav';
import { 
  getNotificationPermission, 
  requestNotificationPermission, 
  checkAndTriggerAlerts, 
  sendSystemNotification 
} from '../utils/notifications';

interface HomeDashboardProps {
  data: DashboardResponse | null;
  loading: boolean;
  onNavigate: (tab: TabType) => void;
}

export const HomeDashboard: React.FC<HomeDashboardProps> = ({ data, loading, onNavigate }) => {
  const [morningEnabled, setMorningEnabled] = useState<boolean>(
    data?.user?.morning_alerts_enabled ?? true
  );
  const [toggling, setToggling] = useState<boolean>(false);
  const [isOnline, setIsOnline] = useState<boolean>(
    typeof navigator !== 'undefined' ? navigator.onLine : true
  );
  const [alertPermission, setAlertPermission] = useState<NotificationPermission>(
    getNotificationPermission()
  );

  // Sync state when data loads or updates
  useEffect(() => {
    if (data?.user?.morning_alerts_enabled !== undefined) {
      setMorningEnabled(data.user.morning_alerts_enabled);
    }
  }, [data?.user?.morning_alerts_enabled]);

  // Monitor network connectivity changes
  useEffect(() => {
    const handleOnline = () => {
      setIsOnline(true);
      if (data?.next_class || data?.urgent_task_preview) {
        checkAndTriggerAlerts(data?.next_class, data?.urgent_task_preview);
      }
    };
    const handleOffline = () => setIsOnline(false);

    window.addEventListener('online', handleOnline);
    window.addEventListener('offline', handleOffline);
    return () => {
      window.removeEventListener('online', handleOnline);
      window.removeEventListener('offline', handleOffline);
    };
  }, [data]);

  // Check alerts on data update
  useEffect(() => {
    if (data && isOnline) {
      checkAndTriggerAlerts(data.next_class, data.urgent_task_preview);
    }
  }, [data, isOnline]);

  const handleToggleMorning = async () => {
    if (toggling) return;
    const nextVal = !morningEnabled;
    setMorningEnabled(nextVal); // Optimistic UI toggle
    setToggling(true);
    try {
      const res = await toggleMorningAlerts(nextVal);
      setMorningEnabled(res);
      if (window.Telegram?.WebApp?.HapticFeedback) {
        window.Telegram.WebApp.HapticFeedback.impactOccurred('medium');
      }
    } catch (err: any) {
      console.error('Failed to toggle morning alert:', err);
      setMorningEnabled(!nextVal); // Rollback on failure
      const errorMsg = err?.response?.data?.detail || err?.message || 'Failed to update setting';
      alert(`Could not toggle morning alert: ${errorMsg}`);
    } finally {
      setToggling(false);
    }
  };

  const handleEnableAlerts = async () => {
    const granted = await requestNotificationPermission();
    setAlertPermission(granted ? 'granted' : 'denied');
    if (granted) {
      sendSystemNotification(
        '🔔 Alerts Active',
        'Your private companion will alert you for upcoming classes & LMS deadlines when connected!'
      );
      if (data) {
        checkAndTriggerAlerts(data.next_class, data.urgent_task_preview);
      }
    }
  };

  const studentName = data?.user?.first_name || 'Dheeraj';
  const studentId = data?.user?.student_id || '2400032903';
  const nextClass = data?.next_class;
  const urgentTask = data?.urgent_task_preview;
  const erpConnected = data?.user?.erp?.connected ?? true;
  const lmsConnected = data?.user?.lms?.connected ?? true;

  return (
    <div className="p-4 space-y-4 max-w-md mx-auto pb-24">
      {/* Network & Live Sync Indicator */}
      <div className="flex items-center justify-between px-1">
        <div className="flex items-center gap-1.5 text-xs">
          {isOnline ? (
            <>
              <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
              <span className="text-emerald-300 font-medium">Campus Network Connected</span>
            </>
          ) : (
            <>
              <span className="w-2 h-2 rounded-full bg-amber-400" />
              <span className="text-amber-300 font-medium">Offline • Secure Local Cache</span>
            </>
          )}
        </div>

        {alertPermission !== 'granted' ? (
          <button
            onClick={handleEnableAlerts}
            className="flex items-center gap-1 text-[11px] font-semibold text-brand-300 bg-brand-violet/20 border border-brand-violet/40 px-2 py-0.5 rounded-lg active:scale-95 transition-all"
          >
            <BellRing size={12} />
            <span>Enable Alerts</span>
          </button>
        ) : (
          <span className="flex items-center gap-1 text-[11px] text-slate-400">
            <Zap size={11} className="text-emerald-400" />
            <span>Alerts Active</span>
          </span>
        )}
      </div>

      {/* Hero Card: DHEERAJ'S PRIVATE ACADEMIC OVERVIEW */}
      <div className="p-4 rounded-2xl bg-gradient-to-b from-[#181232] via-[#111528] to-[#0E1322] border border-brand-violet/30 space-y-3.5 shadow-elevated">
        <div>
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-bold tracking-wider text-brand-400 uppercase">
              PRIVATE ACADEMIC COMPANION
            </span>
            <span className="text-[10px] font-mono bg-emerald-500/10 border border-emerald-500/30 text-emerald-300 px-1.5 py-0.5 rounded-md flex items-center gap-1">
              <ShieldCheck size={11} />
              <span>Personal Vault</span>
            </span>
          </div>

          <h2 className="text-lg font-bold text-white tracking-tight mt-1">
            Welcome back, {studentName}
          </h2>
          <p className="text-xs text-slate-400 font-mono mt-0.5">
            Student ID: <span className="text-white font-semibold">{studentId}</span>
          </p>
        </div>

        {/* 2 Sub-tiles */}
        <div className="grid grid-cols-2 gap-2.5 pt-1">
          {/* Timetable sub-tile */}
          <button
            onClick={() => onNavigate('timetable')}
            className="p-3 rounded-xl bg-[#090D18]/90 border border-white/5 text-left hover:border-brand-violet/40 transition-all group"
          >
            <div className="flex items-center gap-1.5 text-slate-300 text-xs font-medium mb-1.5">
              <Calendar size={14} className="text-slate-400 group-hover:text-brand-400 transition-colors" />
              <span>Timetable</span>
            </div>
            <div className="text-xs font-semibold text-brand-400 group-hover:text-brand-300 flex items-center gap-1">
              <span>View schedule</span>
              <ChevronRight size={12} />
            </div>
          </button>

          {/* Assignments sub-tile */}
          <button
            onClick={() => onNavigate('tasks')}
            className="p-3 rounded-xl bg-[#090D18]/90 border border-white/5 text-left hover:border-brand-violet/40 transition-all group"
          >
            <div className="flex items-center gap-1.5 text-slate-300 text-xs font-medium mb-1.5">
              <CheckSquare size={14} className="text-slate-400 group-hover:text-brand-400 transition-colors" />
              <span>Assignments</span>
            </div>
            <div className="text-xs font-semibold text-brand-400 group-hover:text-brand-300 flex items-center gap-1">
              <span>View tasks</span>
              <ChevronRight size={12} />
            </div>
          </button>
        </div>
      </div>

      {/* Connected Services Card */}
      <div className="space-y-2">
        <h3 className="text-xs font-semibold text-slate-400 uppercase tracking-wider px-1">
          Connected services
        </h3>

        <div className="p-3.5 rounded-2xl bg-[#0E1322] border border-white/5 divide-y divide-white/5 space-y-3">
          {/* ERP Row */}
          <div className="flex items-center justify-between pt-0.5">
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-lg bg-white/5 flex items-center justify-center text-slate-300">
                <GraduationCap size={16} />
              </div>
              <div>
                <div className="text-sm font-medium text-white">University ERP</div>
                <div className="text-xs text-slate-500">Timetable & outpass ({studentId})</div>
              </div>
            </div>
            <span className={`text-xs font-medium ${erpConnected ? 'text-emerald-400' : 'text-slate-500'}`}>
              {erpConnected ? 'Active' : 'Not Linked'}
            </span>
          </div>

          {/* Moodle LMS Row */}
          <div className="flex items-center justify-between pt-3">
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-lg bg-white/5 flex items-center justify-center text-slate-300">
                <BookOpen size={16} />
              </div>
              <div>
                <div className="text-sm font-medium text-white">Moodle LMS</div>
                <div className="text-xs text-slate-500">Assignments & deadlines ({studentId})</div>
              </div>
            </div>
            <span className={`text-xs font-medium ${lmsConnected ? 'text-emerald-400' : 'text-slate-500'}`}>
              {lmsConnected ? 'Connected' : 'Not Linked'}
            </span>
          </div>

          {/* Morning Timetable Row with Toggle */}
          <div className="flex items-center justify-between pt-3">
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-lg bg-white/5 flex items-center justify-center text-slate-300">
                <Bell size={16} />
              </div>
              <div>
                <div className="text-sm font-medium text-white">Morning timetable</div>
                <div className="text-xs text-slate-500">Daily delivery • 07:00 IST</div>
              </div>
            </div>
            <button
              onClick={handleToggleMorning}
              disabled={toggling}
              className={`w-10 h-5.5 flex items-center rounded-full p-0.5 transition-colors ${
                morningEnabled ? 'bg-brand-violet' : 'bg-slate-700'
              }`}
            >
              <div
                className={`bg-white w-4.5 h-4.5 rounded-full shadow-md transform transition-transform ${
                  morningEnabled ? 'translate-x-4.5' : 'translate-x-0'
                }`}
              />
            </button>
          </div>
        </div>

        <p className="text-[11px] text-slate-500 px-1 pt-0.5">
          Real-time sync enabled with your university ERP & LMS accounts.
        </p>
      </div>

      {/* Live Next Class Card */}
      {nextClass && (
        <div className="p-4 rounded-2xl bg-[#0E1322] border border-white/5 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-brand-300 uppercase tracking-wide">
              {nextClass.status === 'in_progress' ? 'Class In Progress' : 'Next Class Today'}
            </span>
            <span className="text-[10px] px-2 py-0.5 rounded-full bg-brand-violet/20 border border-brand-violet/30 text-brand-200">
              {nextClass.time_badge}
            </span>
          </div>
          <h4 className="text-sm font-semibold text-white leading-snug">
            {nextClass.subject_title || nextClass.subject_code}
          </h4>
          <div className="text-xs text-slate-400 flex items-center gap-3 pt-1">
            <span className="flex items-center gap-1"><Clock size={12} className="text-brand-400" /> {nextClass.start_time} - {nextClass.end_time}</span>
            <span className="flex items-center gap-1"><MapPin size={12} className="text-brand-400" /> Room {nextClass.room}</span>
          </div>
        </div>
      )}

      {/* Urgent Task Preview */}
      {urgentTask && (
        <div className="p-4 rounded-2xl bg-[#0E1322] border border-white/5 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-amber-400">
              Upcoming Deadline
            </span>
            <span className="text-[10px] px-2 py-0.5 rounded-full bg-amber-500/20 text-amber-300 border border-amber-500/30">
              {urgentTask.time_left_str}
            </span>
          </div>
          <h4 className="text-sm font-semibold text-white leading-snug">
            {urgentTask.title}
          </h4>
          <div className="text-xs text-slate-400">{urgentTask.course_name}</div>
        </div>
      )}
    </div>
  );
};
