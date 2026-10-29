import React, { useState, useEffect } from 'react';
import { 
  Clock, 
  MapPin, 
  Users, 
  RefreshCw, 
  CalendarDays, 
  AlertCircle,
  Sparkles
} from 'lucide-react';
import { TimetableResponse, DaySchedule, ClassSlot, fetchTimetable, refreshTimetable } from '../api/client';

export const TimetableScreen: React.FC = () => {
  const [data, setData] = useState<TimetableResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [selectedDay, setSelectedDay] = useState<string>('Mon');
  const [error, setError] = useState<string | null>(null);

  const daysOrder = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

  const loadData = async (force = false) => {
    if (force) setRefreshing(true);
    else setLoading(true);
    setError(null);

    try {
      const res = force ? await refreshTimetable() : await fetchTimetable();
      setData(res);
      if (res.current_day && daysOrder.includes(res.current_day)) {
        setSelectedDay(res.current_day);
      } else if (res.days.length > 0) {
        const todayDay = res.days.find((d) => d.is_today);
        if (todayDay) setSelectedDay(todayDay.day);
        else setSelectedDay(res.days[0].day);
      }
    } catch (err: any) {
      setError(err?.response?.data?.detail || err.message || 'Failed to load timetable');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => {
    loadData();
  }, []);

  const currentDaySchedule: DaySchedule | undefined = data?.days.find(
    (d) => d.day.toLowerCase() === selectedDay.toLowerCase()
  );

  return (
    <div className="p-4 space-y-4 max-w-md mx-auto pb-24">
      {/* Top Header */}
      <div className="flex items-center gap-2">
        <CalendarDays size={20} className="text-brand-400" />
        <h2 className="text-lg font-bold text-white tracking-tight leading-tight">Academic Schedule</h2>
      </div>

      {/* Day Selector Tabs Grid (Exact 6-column layout from screenshot) */}
      <div className="grid grid-cols-6 gap-2">
        {daysOrder.map((day) => {
          const isSelected = selectedDay.toLowerCase() === day.toLowerCase();
          const dayData = data?.days.find((d) => d.day.toLowerCase() === day.toLowerCase());

          return (
            <button
              key={day}
              onClick={() => {
                if (window.Telegram?.WebApp?.HapticFeedback) {
                  window.Telegram.WebApp.HapticFeedback.impactOccurred('light');
                }
                setSelectedDay(day);
              }}
              className={`flex flex-col items-center justify-center py-2.5 px-1 rounded-2xl transition-all ${
                isSelected
                  ? 'bg-[#7C3AED] text-white shadow-lg shadow-[#7C3AED]/25'
                  : 'bg-[#0E1322] text-slate-400 hover:text-slate-200 border border-white/5'
              }`}
            >
              <span className="text-xs font-semibold">{day}</span>
              <span className="text-[11px] opacity-80 mt-0.5">
                {dayData ? `${dayData.total_classes}c` : '0c'}
              </span>
              {isSelected && (
                <span className="w-1.5 h-1.5 rounded-full bg-white mt-1" />
              )}
            </button>
          );
        })}
      </div>

      {/* Loading Skeleton */}
      {loading && !data && (
        <div className="space-y-3 animate-pulse">
          <div className="h-24 bg-white/5 rounded-2xl" />
          <div className="h-24 bg-white/5 rounded-2xl" />
          <div className="h-24 bg-white/5 rounded-2xl" />
        </div>
      )}

      {/* Error Banner */}
      {error && (
        <div className="p-3.5 rounded-xl bg-rose-500/10 border border-rose-500/20 text-xs text-rose-300 flex items-start gap-2.5">
          <AlertCircle size={16} className="text-rose-400 shrink-0 mt-0.5" />
          <div>
            <div className="font-semibold">Unable to load timetable</div>
            <div className="text-rose-300/80 mt-0.5">{error}</div>
            <button
              onClick={() => loadData(true)}
              className="mt-2 px-2.5 py-1 rounded bg-rose-500/20 hover:bg-rose-500/30 font-medium text-[11px]"
            >
              Retry Sync
            </button>
          </div>
        </div>
      )}

      {/* Class Cards List */}
      {!loading && currentDaySchedule && currentDaySchedule.classes.length > 0 ? (
        <div className="space-y-3">
          <div className="text-[11px] font-medium text-slate-400 px-1 flex items-center justify-between">
            <span>{currentDaySchedule.full_day_name}</span>
            <span>{currentDaySchedule.total_classes} scheduled classes</span>
          </div>

          {currentDaySchedule.classes.map((cls, idx) => (
            <div
              key={`${cls.subject_code}-${idx}`}
              className="p-3.5 rounded-2xl bg-[#111625] border border-white/5 hover:border-brand-500/20 transition-all space-y-2 glow-card"
            >
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-brand-300 flex items-center gap-1.5 font-mono">
                  <Clock size={13} className="text-brand-400" />
                  <span>{cls.start_time} - {cls.end_time}</span>
                </span>
                <span className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-white/5 border border-white/10 text-slate-300">
                  {cls.class_type}
                </span>
              </div>

              <div>
                <h3 className="text-sm font-semibold text-white leading-snug">
                  {cls.subject_title || cls.subject_code}
                </h3>
                <div className="text-xs text-slate-400 font-mono mt-0.5">
                  {cls.subject_code}
                </div>
              </div>

              <div className="pt-2 border-t border-white/5 flex items-center justify-between text-xs text-slate-300">
                <div className="flex items-center gap-1.5">
                  <MapPin size={13} className="text-brand-400" />
                  <span className="font-medium text-white">{cls.room}</span>
                </div>
                <div className="flex items-center gap-1.5 text-slate-400">
                  <Users size={13} />
                  <span>{cls.section}</span>
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : !loading && (
        <div className="py-16 text-center rounded-2xl bg-[#0E1322] border border-white/5 p-6 flex flex-col items-center justify-center min-h-[300px]">
          <div className="w-12 h-12 rounded-full bg-white/5 flex items-center justify-center mx-auto mb-3.5 text-slate-400">
            <CalendarDays size={22} />
          </div>
          <h3 className="text-base font-bold text-white">No Classes Scheduled</h3>
          <p className="text-xs text-slate-400 mt-1.5 max-w-xs mx-auto leading-relaxed">
            You have no classes or laboratory sessions on this day.
          </p>
        </div>
      )}
    </div>
  );
};

