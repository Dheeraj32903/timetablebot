import React, { useState, useEffect } from 'react';
import { 
  CheckSquare, 
  Clock, 
  AlertCircle, 
  RefreshCw, 
  ExternalLink, 
  CheckCircle2, 
  Search,
  Filter
} from 'lucide-react';
import { LMSTasksResponse, LMSTaskItem, fetchLMSTasks, refreshLMSTasks } from '../api/client';

export const AssignmentsScreen: React.FC = () => {
  const [data, setData] = useState<LMSTasksResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [filter, setFilter] = useState<'all' | 'urgent' | 'due_soon' | 'submitted'>('all');
  const [error, setError] = useState<string | null>(null);

  const loadData = async (force = false) => {
    if (force) setRefreshing(true);
    else setLoading(true);
    setError(null);

    try {
      const res = force ? await refreshLMSTasks() : await fetchLMSTasks();
      setData(res);
    } catch (err: any) {
      setError(err?.response?.data?.detail || err.message || 'Failed to load LMS assignments');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => {
    loadData();
  }, []);

  const tasks = data?.tasks || [];
  const filteredTasks = tasks.filter((t) => {
    if (filter === 'all') return !t.is_submitted;
    if (filter === 'urgent') return !t.is_submitted && t.urgency === 'urgent';
    if (filter === 'due_soon') return !t.is_submitted && (t.urgency === 'urgent' || t.urgency === 'due_soon');
    if (filter === 'submitted') return t.is_submitted;
    return true;
  });

  const getTimeBadgeText = (task: LMSTaskItem) => {
    if (task.is_submitted) return 'Submitted';
    if (task.seconds_remaining <= 0) return 'Past Due';
    if (task.seconds_remaining <= 86400) return `Due Today (${task.time_left_str})`;
    if (task.seconds_remaining <= 172800) return `Due Tomorrow (${task.time_left_str})`;
    return `Due in ${task.time_left_str}`;
  };

  return (
    <div className="p-4 space-y-4 max-w-md mx-auto pb-24">
      {/* Header */}
      <div className="flex items-center gap-2">
        <CheckSquare size={20} className="text-brand-400" />
        <h2 className="text-lg font-bold text-white tracking-tight">Assignments & Quizzes</h2>
      </div>

      {/* Metrics Row */}
      <div className="grid grid-cols-3 gap-2.5">
        <div className="p-3 rounded-xl bg-[#111625] border border-white/5">
          <div className="text-[11px] text-slate-400 font-medium">Pending</div>
          <div className="text-lg font-bold text-white mt-0.5">{data?.total_pending ?? 0}</div>
        </div>
        <div className="p-3 rounded-xl bg-[#111625] border border-white/5">
          <div className="text-[11px] text-rose-400 font-medium flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-rose-400" />
            <span>Due Today</span>
          </div>
          <div className="text-lg font-bold text-rose-300 mt-0.5">{data?.urgent_count ?? 0}</div>
        </div>
        <div className="p-3 rounded-xl bg-[#111625] border border-white/5">
          <div className="text-[11px] text-emerald-400 font-medium flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
            <span>Completed</span>
          </div>
          <div className="text-lg font-bold text-emerald-300 mt-0.5">{data?.total_submitted ?? 0}</div>
        </div>
      </div>

      {/* Filter Tabs */}
      <div className="flex gap-1.5 bg-[#111625] p-1 rounded-xl border border-white/5 text-xs">
        <button
          onClick={() => setFilter('all')}
          className={`flex-1 py-1.5 rounded-lg font-medium transition-all ${
            filter === 'all' ? 'bg-brand-violet text-white shadow-sm' : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          Pending
        </button>
        <button
          onClick={() => setFilter('urgent')}
          className={`flex-1 py-1.5 rounded-lg font-medium transition-all ${
            filter === 'urgent' ? 'bg-brand-violet text-white shadow-sm' : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          Urgent
        </button>
        <button
          onClick={() => setFilter('due_soon')}
          className={`flex-1 py-1.5 rounded-lg font-medium transition-all ${
            filter === 'due_soon' ? 'bg-brand-violet text-white shadow-sm' : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          Due Soon
        </button>
        <button
          onClick={() => setFilter('submitted')}
          className={`flex-1 py-1.5 rounded-lg font-medium transition-all ${
            filter === 'submitted' ? 'bg-brand-violet text-white shadow-sm' : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          Submitted
        </button>
      </div>

      {/* Loading Skeleton */}
      {loading && !data && (
        <div className="space-y-3 animate-pulse">
          <div className="h-24 bg-white/5 rounded-2xl" />
          <div className="h-24 bg-white/5 rounded-2xl" />
          <div className="h-24 bg-white/5 rounded-2xl" />
        </div>
      )}

      {/* Error Message */}
      {error && (
        <div className="p-3.5 rounded-xl bg-rose-500/10 border border-rose-500/20 text-xs text-rose-300 flex items-start gap-2.5">
          <AlertCircle size={16} className="text-rose-400 shrink-0 mt-0.5" />
          <div>
            <div className="font-semibold">LMS Error</div>
            <div className="text-rose-300/80 mt-0.5">{error}</div>
          </div>
        </div>
      )}

      {/* Tasks List */}
      {!loading && filteredTasks.length > 0 ? (
        <div className="space-y-3">
          {filteredTasks.map((t) => (
            <div
              key={t.id}
              className="p-3.5 rounded-2xl bg-[#111625] border border-white/5 hover:border-brand-500/20 transition-all space-y-2 glow-card"
            >
              <div className="flex items-center justify-between">
                <span className="text-[11px] font-medium text-slate-400 truncate max-w-[200px]">
                  {t.course_name}
                </span>
                <span
                  className={`text-[10px] font-medium px-2 py-0.5 rounded-full flex items-center gap-1 ${
                    t.is_submitted
                      ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30'
                      : t.urgency === 'urgent'
                      ? 'bg-rose-500/20 text-rose-300 border border-rose-500/30'
                      : t.urgency === 'due_soon'
                      ? 'bg-amber-500/20 text-amber-300 border border-amber-500/30'
                      : 'bg-white/5 text-slate-300 border border-white/10'
                  }`}
                >
                  <span
                    className={`w-1.5 h-1.5 rounded-full ${
                      t.is_submitted
                        ? 'bg-emerald-400'
                        : t.urgency === 'urgent'
                        ? 'bg-rose-400'
                        : t.urgency === 'due_soon'
                        ? 'bg-amber-400'
                        : 'bg-slate-400'
                    }`}
                  />
                  <span>{getTimeBadgeText(t)}</span>
                </span>
              </div>

              <div>
                <h3 className="text-sm font-semibold text-white leading-snug">
                  {t.title}
                </h3>
                <div className="text-[11px] text-slate-500 mt-1 flex items-center gap-1">
                  <Clock size={11} />
                  <span>Due {t.due_date_str}</span>
                </div>
              </div>

              {t.url && (
                <div className="pt-2 border-t border-white/5 flex justify-end">
                  <a
                    href={t.url}
                    target="_blank"
                    rel="noreferrer"
                    className="py-1 px-2.5 rounded-lg bg-white/5 hover:bg-white/10 text-xs font-medium text-brand-300 flex items-center gap-1.5 transition-colors"
                  >
                    <span>Open Submission</span>
                    <ExternalLink size={11} />
                  </a>
                </div>
              )}
            </div>
          ))}
        </div>
      ) : !loading && (
        <div className="py-12 text-center rounded-2xl bg-[#111625] border border-white/5 p-6">
          <div className="w-12 h-12 rounded-full bg-white/5 flex items-center justify-center mx-auto mb-3 text-emerald-400">
            <CheckCircle2 size={22} />
          </div>
          <h3 className="text-sm font-semibold text-white">All Clear</h3>
          <p className="text-xs text-slate-400 mt-1 max-w-xs mx-auto">
            {filter === 'submitted'
              ? 'No completed assignments found.'
              : 'You have no pending assignments or quizzes in this category!'}
          </p>
        </div>
      )}
    </div>
  );
};

