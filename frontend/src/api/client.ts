import axios from 'axios';

// Declare Telegram WebApp types
declare global {
  interface Window {
    Telegram?: {
      WebApp?: {
        initData: string;
        initDataUnsafe?: {
          user?: {
            id: number;
            first_name: string;
            last_name?: string;
            username?: string;
          };
        };
        ready: () => void;
        expand: () => void;
        close: () => void;
        setHeaderColor: (color: string) => void;
        setBackgroundColor: (color: string) => void;
        HapticFeedback?: {
          impactOccurred: (style: 'light' | 'medium' | 'heavy' | 'rigid' | 'soft') => void;
          notificationOccurred: (type: 'error' | 'success' | 'warning') => void;
        };
      };
    };
  }
}

export const getTelegramInitData = (): string => {
  if (typeof window !== 'undefined' && window.Telegram?.WebApp?.initData) {
    return window.Telegram.WebApp.initData;
  }
  return 'dev';
};

export const getApiBaseUrl = (): string => {
  if (typeof window === 'undefined') return '';
  const custom = localStorage.getItem('klu_backend_url');
  if (custom) return custom.replace(/\/+$/, '');
  
  // If running inside Capacitor native container or standalone APK:
  const isCapacitor = !!(window as any).Capacitor?.isNativePlatform?.();
  if (isCapacitor || window.location.protocol === 'file:' || (window.location.hostname === 'localhost' && window.location.port !== '8000')) {
    return 'https://timetablebot-1e9m.onrender.com';
  }
  return '';
};

const apiClient = axios.create({
  baseURL: '',
  timeout: 45000,
});

apiClient.interceptors.request.use((config) => {
  const base = getApiBaseUrl();
  if (base && !config.baseURL) {
    config.baseURL = base;
  }
  const initData = getTelegramInitData();
  if (initData) {
    config.headers['X-Telegram-Init-Data'] = initData;
  }
  return config;
});

// --- Type Definitions ---

export interface IntegrationStatus {
  connected: boolean;
  username?: string | null;
  status_text: string;
  details?: string | null;
}

export interface UserProfile {
  id: number;
  first_name: string;
  last_name: string;
  telegram_username: string;
  student_id: string | null;
  erp: IntegrationStatus;
  lms: IntegrationStatus;
  morning_alerts_enabled: boolean;
  morning_alert_time: string;
}

export interface ClassSlot {
  slot_label: string;
  start_time: string;
  end_time: string;
  subject_code: string;
  subject_title?: string;
  class_type: string;
  room: string;
  section: string;
}

export interface DaySchedule {
  day: string;
  full_day_name: string;
  total_classes: number;
  is_today: boolean;
  classes: ClassSlot[];
}

export interface TimetableResponse {
  current_day: string;
  has_timetable: boolean;
  is_live_synced: boolean;
  days: DaySchedule[];
  error_message?: string | null;
}

export interface LMSTaskItem {
  id: string;
  title: string;
  course_name: string;
  task_type: string;
  due_timestamp: number;
  due_date_str: string;
  time_left_str: string;
  seconds_remaining: number;
  is_submitted: boolean;
  urgency: 'urgent' | 'due_soon' | 'upcoming' | 'submitted';
  url: string;
}

export interface LMSTasksResponse {
  is_connected: boolean;
  student_name: string | null;
  total_pending: number;
  total_submitted: number;
  urgent_count: number;
  tasks: LMSTaskItem[];
  error_message?: string | null;
}

export interface OutpassRecord {
  id: string;
  student_id: string;
  outing_date: string;
  out_time: string;
  in_time: string;
  reason: string;
  warden_name?: string | null;
  status: string;
  approved_by?: string | null;
  cancellation_status?: string | null;
  view_url?: string | null;
}

export interface OutpassHistoryResponse {
  has_active: boolean;
  active_outpass: OutpassRecord | null;
  history: OutpassRecord[];
  error_message?: string | null;
}

export interface NextClassInfo {
  subject_title: string;
  subject_code: string;
  class_type: string;
  room: string;
  section: string;
  start_time: string;
  end_time: string;
  status: 'in_progress' | 'upcoming' | 'completed';
  time_badge: string;
}

export interface DashboardResponse {
  user: UserProfile;
  next_class: NextClassInfo | null;
  classes_today_count: number;
  pending_tasks_count: number;
  urgent_tasks_count: number;
  urgent_task_preview: LMSTaskItem | null;
  active_outpass: OutpassRecord | null;
  today_day_name: string;
}

export interface LocalOutpassPayload {
  date: string;
  out_time: string;
  in_time: string;
  reason: string;
  meals: {
    breakfast: string;
    lunch: string;
    snack: string;
    dinner: string;
  };
}

export interface HomeOutpassPayload {
  out_datetime: string;
  in_datetime: string;
  reason: string;
}

// --- API Service Functions with Offline-First Caching ---

const CACHE_DASHBOARD_KEY = 'klu_private_cache_dashboard';
const CACHE_TIMETABLE_KEY = 'klu_private_cache_timetable';
const CACHE_TASKS_KEY = 'klu_private_cache_tasks';

export const fetchDashboard = async (): Promise<DashboardResponse> => {
  try {
    const res = await apiClient.get<DashboardResponse>('/api/dashboard');
    if (res.data) {
      localStorage.setItem(CACHE_DASHBOARD_KEY, JSON.stringify(res.data));
    }
    return res.data;
  } catch (err) {
    const cached = localStorage.getItem(CACHE_DASHBOARD_KEY);
    if (cached) {
      console.info('Loaded Dashboard from secure offline cache');
      return JSON.parse(cached);
    }
    throw err;
  }
};

export const fetchProfile = async (): Promise<UserProfile> => {
  const res = await apiClient.get<UserProfile>('/api/me');
  return res.data;
};

export const fetchTimetable = async (forceRefresh = false): Promise<TimetableResponse> => {
  try {
    const res = await apiClient.get<TimetableResponse>(`/api/timetable?force_refresh=${forceRefresh}`);
    if (res.data) {
      localStorage.setItem(CACHE_TIMETABLE_KEY, JSON.stringify(res.data));
    }
    return res.data;
  } catch (err) {
    const cached = localStorage.getItem(CACHE_TIMETABLE_KEY);
    if (cached) {
      console.info('Loaded Timetable from secure offline cache');
      return JSON.parse(cached);
    }
    throw err;
  }
};

export const refreshTimetable = async (): Promise<TimetableResponse> => {
  const res = await apiClient.post<TimetableResponse>('/api/timetable/refresh');
  if (res.data) {
    localStorage.setItem(CACHE_TIMETABLE_KEY, JSON.stringify(res.data));
  }
  return res.data;
};

export const fetchLMSTasks = async (forceRefresh = false): Promise<LMSTasksResponse> => {
  try {
    const res = await apiClient.get<LMSTasksResponse>(`/api/lms/tasks?force_refresh=${forceRefresh}`);
    if (res.data) {
      localStorage.setItem(CACHE_TASKS_KEY, JSON.stringify(res.data));
    }
    return res.data;
  } catch (err) {
    const cached = localStorage.getItem(CACHE_TASKS_KEY);
    if (cached) {
      console.info('Loaded LMS Tasks from secure offline cache');
      return JSON.parse(cached);
    }
    throw err;
  }
};

export const refreshLMSTasks = async (): Promise<LMSTasksResponse> => {
  const res = await apiClient.post<LMSTasksResponse>('/api/lms/refresh');
  if (res.data) {
    localStorage.setItem(CACHE_TASKS_KEY, JSON.stringify(res.data));
  }
  return res.data;
};

export const fetchOutpassHistory = async (): Promise<OutpassHistoryResponse> => {
  const res = await apiClient.get<OutpassHistoryResponse>('/api/outpass/history');
  return res.data;
};

export const fetchAllowedInTimes = async (outTime: string): Promise<string[]> => {
  const res = await apiClient.get<{ out_time: string; allowed_times: string[] }>(
    `/api/outpass/allowed-times?out_time=${encodeURIComponent(outTime)}`
  );
  return res.data.allowed_times;
};

export const submitLocalOutpass = async (payload: LocalOutpassPayload): Promise<{ success: boolean; message: string }> => {
  const res = await apiClient.post<{ success: boolean; message: string }>('/api/outpass/submit-local', payload);
  return res.data;
};

export const submitHomeOutpass = async (payload: HomeOutpassPayload): Promise<{ success: boolean; message: string }> => {
  const res = await apiClient.post<{ success: boolean; message: string }>('/api/outpass/submit-home', payload);
  return res.data;
};

export const toggleMorningAlerts = async (enabled: boolean): Promise<boolean> => {
  const res = await apiClient.post<{ success: boolean; morning_alerts_enabled: boolean }>(
    '/api/settings/morning',
    { enabled }
  );
  return res.data.morning_alerts_enabled;
};
