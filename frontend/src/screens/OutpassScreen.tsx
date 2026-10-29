import React, { useState, useEffect } from 'react';
import { 
  Ticket, 
  Clock, 
  Calendar, 
  MapPin, 
  AlertCircle, 
  CheckCircle2, 
  ShieldCheck, 
  ArrowRight,
  Info,
  ChevronRight,
  RefreshCw
} from 'lucide-react';
import { 
  OutpassHistoryResponse, 
  OutpassRecord, 
  fetchOutpassHistory, 
  fetchAllowedInTimes, 
  submitLocalOutpass, 
  submitHomeOutpass 
} from '../api/client';

export const OutpassScreen: React.FC = () => {
  const [historyData, setHistoryData] = useState<OutpassHistoryResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [mode, setMode] = useState<'local' | 'home'>('local');

  // Form State - Local Outing
  const todayStr = new Date().toISOString().split('T')[0];
  const [localDate, setLocalDate] = useState<string>(todayStr);
  const [localOutTime, setLocalOutTime] = useState<string>('17:00:00');
  const [localInTime, setLocalInTime] = useState<string>('20:30:00');
  const [allowedInTimes, setAllowedInTimes] = useState<string[]>([]);
  const [loadingAllowedTimes, setLoadingAllowedTimes] = useState<boolean>(false);
  const [localReason, setLocalReason] = useState<string>('');
  const [meals, setMeals] = useState<{ breakfast: boolean; lunch: boolean; snack: boolean; dinner: boolean }>({
    breakfast: false,
    lunch: false,
    snack: false,
    dinner: false,
  });

  // Form State - Home Outing
  const [homeOutDate, setHomeOutDate] = useState<string>(`${todayStr}T17:00`);
  const [homeInDate, setHomeInDate] = useState<string>(`${todayStr}T21:00`);
  const [homeReason, setHomeReason] = useState<string>('');

  // Submission & Confirmation Modal State
  const [showReviewModal, setShowReviewModal] = useState<boolean>(false);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [submitResult, setSubmitResult] = useState<{ success: boolean; message: string } | null>(null);

  const loadHistory = async () => {
    setLoading(true);
    try {
      const res = await fetchOutpassHistory();
      setHistoryData(res);
    } catch (err: any) {
      console.error('Failed to load outpass history:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadHistory();
  }, []);

  // Update allowed in-times whenever out-time changes in local outing
  useEffect(() => {
    if (mode === 'local') {
      const fetchTimes = async () => {
        setLoadingAllowedTimes(true);
        try {
          const times = await fetchAllowedInTimes(localOutTime);
          setAllowedInTimes(times);
          if (times.length > 0 && !times.includes(localInTime)) {
            setLocalInTime(times[0]);
          }
        } catch (err) {
          console.error('Error fetching allowed in-times:', err);
        } finally {
          setLoadingAllowedTimes(false);
        }
      };
      fetchTimes();
    }
  }, [localOutTime, mode]);

  const handleOpenReview = (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitResult(null);

    const reason = mode === 'local' ? localReason.trim() : homeReason.trim();
    if (reason.length < 2) {
      alert('Please enter a valid reason (minimum 2 characters).');
      return;
    }

    setShowReviewModal(true);
  };

  const handleConfirmSubmit = async () => {
    setSubmitting(true);
    setSubmitResult(null);

    try {
      let res;
      if (mode === 'local') {
        res = await submitLocalOutpass({
          date: localDate,
          out_time: localOutTime,
          in_time: localInTime,
          reason: localReason.trim(),
          meals: {
            breakfast: meals.breakfast ? 'YES' : 'NO',
            lunch: meals.lunch ? 'YES' : 'NO',
            snack: meals.snack ? 'YES' : 'NO',
            dinner: meals.dinner ? 'YES' : 'NO',
          },
        });
      } else {
        const outFmt = homeOutDate.replace('T', ' ') + ':00';
        const inFmt = homeInDate.replace('T', ' ') + ':00';
        res = await submitHomeOutpass({
          out_datetime: outFmt,
          in_datetime: inFmt,
          reason: homeReason.trim(),
        });
      }

      setSubmitResult(res);
      if (res.success) {
        if (window.Telegram?.WebApp?.HapticFeedback) {
          window.Telegram.WebApp.HapticFeedback.notificationOccurred('success');
        }
        setShowReviewModal(false);
        loadHistory();
      } else {
        if (window.Telegram?.WebApp?.HapticFeedback) {
          window.Telegram.WebApp.HapticFeedback.notificationOccurred('error');
        }
      }
    } catch (err: any) {
      setSubmitResult({
        success: false,
        message: err?.response?.data?.detail || err.message || 'Submission failed. Please check ERP.',
      });
    } finally {
      setSubmitting(false);
    }
  };

  const activePass = historyData?.active_outpass;

  return (
    <div className="p-4 space-y-4 max-w-md mx-auto pb-28">
      {/* Top Header */}
      <div className="flex items-center gap-2">
        <Ticket size={20} className="text-brand-400" />
        <h2 className="text-lg font-bold text-white tracking-tight">Hostel Outpass</h2>
      </div>

      {/* Active Outpass Banner */}
      {activePass && (
        <div className="p-4 rounded-2xl bg-gradient-to-br from-emerald-950/40 via-[#111625] to-[#111625] border border-emerald-500/30 space-y-2.5">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-emerald-400 flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
              <span>Active Outpass #{activePass.id}</span>
            </span>
            <span className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">
              {activePass.status}
            </span>
          </div>

          <div className="grid grid-cols-2 gap-2 text-xs pt-1">
            <div>
              <div className="text-[10px] text-slate-400">Departure</div>
              <div className="font-semibold text-white">{activePass.out_time}</div>
            </div>
            <div>
              <div className="text-[10px] text-slate-400">Return By</div>
              <div className="font-semibold text-white">{activePass.in_time}</div>
            </div>
          </div>

          <div className="text-xs text-slate-300 pt-1.5 border-t border-white/5 space-y-0.5">
            <div><span className="text-slate-500">Reason:</span> {activePass.reason}</div>
            {activePass.warden_name && (
              <div><span className="text-slate-500">Warden:</span> {activePass.warden_name}</div>
            )}
            {activePass.approved_by && activePass.approved_by !== 'N/A' && (
              <div><span className="text-slate-500">Approved By:</span> {activePass.approved_by}</div>
            )}
          </div>
        </div>
      )}

      {/* Submission Feedback Message */}
      {submitResult && (
        <div
          className={`p-3.5 rounded-xl text-xs flex items-start gap-2.5 ${
            submitResult.success
              ? 'bg-emerald-500/10 border border-emerald-500/30 text-emerald-300'
              : 'bg-rose-500/10 border border-rose-500/30 text-rose-300'
          }`}
        >
          {submitResult.success ? (
            <CheckCircle2 size={16} className="text-emerald-400 shrink-0 mt-0.5" />
          ) : (
            <AlertCircle size={16} className="text-rose-400 shrink-0 mt-0.5" />
          )}
          <div>
            <div className="font-semibold">{submitResult.success ? 'Success' : 'Submission Note'}</div>
            <div className="mt-0.5 opacity-90">{submitResult.message}</div>
          </div>
        </div>
      )}

      {/* Outing Mode Selector */}
      <div className="flex gap-1.5 bg-[#111625] p-1 rounded-xl border border-white/5 text-xs">
        <button
          onClick={() => setMode('local')}
          className={`flex-1 py-2 rounded-lg font-medium transition-all ${
            mode === 'local' ? 'bg-brand-violet text-white shadow-sm' : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          Local Outing (Hours)
        </button>
        <button
          onClick={() => setMode('home')}
          className={`flex-1 py-2 rounded-lg font-medium transition-all ${
            mode === 'home' ? 'bg-brand-violet text-white shadow-sm' : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          Home Outing (Days)
        </button>
      </div>

      {/* Outpass Form */}
      <form onSubmit={handleOpenReview} className="space-y-3.5">
        {mode === 'local' ? (
          <>
            {/* Date */}
            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1">
                Outing Date
              </label>
              <input
                type="date"
                value={localDate}
                min={todayStr}
                onChange={(e) => setLocalDate(e.target.value)}
                required
                className="w-full px-3 py-2 rounded-xl bg-[#111625] border border-white/10 text-white text-xs focus:outline-none focus:border-brand-500"
              />
            </div>

            {/* Out Time & In Time */}
            <div className="grid grid-cols-2 gap-2.5">
              <div>
                <label className="block text-xs font-medium text-slate-300 mb-1">
                  Out Time
                </label>
                <select
                  value={localOutTime}
                  onChange={(e) => setLocalOutTime(e.target.value)}
                  className="w-full px-3 py-2 rounded-xl bg-[#111625] border border-white/10 text-white text-xs focus:outline-none focus:border-brand-500"
                >
                  <option value="06:00:00">06:00 AM</option>
                  <option value="07:00:00">07:00 AM</option>
                  <option value="08:00:00">08:00 AM</option>
                  <option value="11:00:00">11:00 AM</option>
                  <option value="15:00:00">03:00 PM</option>
                  <option value="16:00:00">04:00 PM</option>
                  <option value="17:00:00">05:00 PM</option>
                  <option value="18:00:00">06:00 PM</option>
                </select>
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-300 mb-1">
                  Return By
                </label>
                <select
                  value={localInTime}
                  onChange={(e) => setLocalInTime(e.target.value)}
                  disabled={loadingAllowedTimes}
                  className="w-full px-3 py-2 rounded-xl bg-[#111625] border border-white/10 text-white text-xs focus:outline-none focus:border-brand-500 disabled:opacity-50"
                >
                  {allowedInTimes.length > 0 ? (
                    allowedInTimes.map((t) => (
                      <option key={t} value={t}>{t}</option>
                    ))
                  ) : (
                    <>
                      <option value="20:00:00">08:00 PM</option>
                      <option value="20:30:00">08:30 PM</option>
                      <option value="21:00:00">09:00 PM</option>
                    </>
                  )}
                </select>
              </div>
            </div>

            {/* Meals Selection */}
            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1.5">
                Hostel Mess Meals Required
              </label>
              <div className="grid grid-cols-4 gap-1.5">
                {(['breakfast', 'lunch', 'snack', 'dinner'] as const).map((m) => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => setMeals({ ...meals, [m]: !meals[m] })}
                    className={`py-1.5 px-2 rounded-lg text-xs font-medium capitalize border transition-all ${
                      meals[m]
                        ? 'bg-brand-violet/20 border-brand-violet text-brand-300'
                        : 'bg-[#111625] border-white/5 text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    {m}
                  </button>
                ))}
              </div>
            </div>

            {/* Reason */}
            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1">
                Reason for Outing
              </label>
              <input
                type="text"
                placeholder="e.g. Doctor appointment, Library, Project materials"
                value={localReason}
                onChange={(e) => setLocalReason(e.target.value)}
                required
                minLength={2}
                maxLength={100}
                className="w-full px-3 py-2 rounded-xl bg-[#111625] border border-white/10 text-white text-xs focus:outline-none focus:border-brand-500 placeholder:text-slate-600"
              />
            </div>
          </>
        ) : (
          <>
            {/* Home Outing Fields */}
            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1">
                Departure Date & Time
              </label>
              <input
                type="datetime-local"
                value={homeOutDate}
                onChange={(e) => setHomeOutDate(e.target.value)}
                required
                className="w-full px-3 py-2 rounded-xl bg-[#111625] border border-white/10 text-white text-xs focus:outline-none focus:border-brand-500"
              />
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1">
                Return Date & Time
              </label>
              <input
                type="datetime-local"
                value={homeInDate}
                onChange={(e) => setHomeInDate(e.target.value)}
                required
                className="w-full px-3 py-2 rounded-xl bg-[#111625] border border-white/10 text-white text-xs focus:outline-none focus:border-brand-500"
              />
            </div>

            <div>
              <label className="block text-xs font-medium text-slate-300 mb-1">
                Reason for Going Home
              </label>
              <input
                type="text"
                placeholder="e.g. Family function, Health recovery, Festival"
                value={homeReason}
                onChange={(e) => setHomeReason(e.target.value)}
                required
                minLength={2}
                maxLength={100}
                className="w-full px-3 py-2 rounded-xl bg-[#111625] border border-white/10 text-white text-xs focus:outline-none focus:border-brand-500 placeholder:text-slate-600"
              />
            </div>
          </>
        )}

        <button
          type="submit"
          className="w-full py-2.5 px-4 rounded-xl bg-gradient-to-r from-brand-violet to-brand-500 hover:opacity-95 text-white font-semibold text-xs shadow-glow-violet active:scale-95 transition-all"
        >
          Review Application
        </button>
      </form>

      {/* Review & Confirmation Modal */}
      {showReviewModal && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-end sm:items-center justify-center p-3">
          <div className="w-full max-w-sm rounded-2xl bg-[#111625] border border-white/10 p-5 space-y-4 shadow-elevated animate-in fade-in zoom-in-95 duration-150">
            <div className="flex items-center gap-2 text-white">
              <ShieldCheck size={18} className="text-brand-400" />
              <h3 className="text-sm font-bold">Confirm Outpass Application</h3>
            </div>

            <div className="p-3 rounded-xl bg-white/5 space-y-2 text-xs">
              <div className="flex justify-between">
                <span className="text-slate-400">Type</span>
                <span className="font-medium text-white">
                  {mode === 'local' ? 'Local Outing (Hours)' : 'Home Outing (Days)'}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-400">Out Time</span>
                <span className="font-medium text-white">
                  {mode === 'local' ? `${localDate} ${localOutTime}` : homeOutDate.replace('T', ' ')}
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-400">Return By</span>
                <span className="font-medium text-white">
                  {mode === 'local' ? `${localDate} ${localInTime}` : homeInDate.replace('T', ' ')}
                </span>
              </div>
              <div className="flex justify-between border-t border-white/5 pt-1.5">
                <span className="text-slate-400">Reason</span>
                <span className="font-medium text-white truncate max-w-[180px]">
                  {mode === 'local' ? localReason : homeReason}
                </span>
              </div>
            </div>

            <p className="text-[11px] text-slate-400">
              This request will be sent to the university ERP hostel gate authority under your student ID.
            </p>

            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => setShowReviewModal(false)}
                disabled={submitting}
                className="flex-1 py-2 rounded-xl bg-white/5 hover:bg-white/10 text-xs font-medium text-slate-300"
              >
                Back
              </button>
              <button
                type="button"
                onClick={handleConfirmSubmit}
                disabled={submitting}
                className="flex-1 py-2 rounded-xl bg-brand-violet hover:bg-brand-violet/90 text-xs font-semibold text-white shadow-glow-violet flex items-center justify-center gap-1.5 disabled:opacity-50"
              >
                {submitting ? (
                  <>
                    <RefreshCw size={13} className="animate-spin" />
                    <span>Submitting...</span>
                  </>
                ) : (
                  <span>Submit Request</span>
                )}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

