// Notification & Network Alert Engine for Dheeraj's Private Android App

export interface AlertNotification {
  id: string;
  title: string;
  body: string;
  type: 'class' | 'deadline' | 'system';
  timestamp: number;
}

const NOTIFIED_CLASSES_KEY = 'klu_notified_classes';
const NOTIFIED_TASKS_KEY = 'klu_notified_tasks';

export const isNotificationSupported = (): boolean => {
  return typeof window !== 'undefined' && 'Notification' in window;
};

export const getNotificationPermission = (): NotificationPermission => {
  if (!isNotificationSupported()) return 'denied';
  return Notification.permission;
};

export const requestNotificationPermission = async (): Promise<boolean> => {
  if (!isNotificationSupported()) return false;
  try {
    const perm = await Notification.requestPermission();
    return perm === 'granted';
  } catch (err) {
    console.warn('Failed to request notification permission:', err);
    return false;
  }
};

export const sendSystemNotification = (title: string, body: string, icon = '/vite.svg'): void => {
  if (!isNotificationSupported() || Notification.permission !== 'granted') return;
  try {
    new Notification(title, {
      body,
      icon,
      badge: icon,
    });
  } catch (err) {
    console.warn('Could not trigger notification:', err);
  }
};

export const checkAndTriggerAlerts = (
  nextClass: { subject_title?: string; subject_code?: string; room?: string; time_badge?: string; start_time?: string; status?: string } | null,
  urgentTask: { id: string; title: string; course_name: string; time_left_str: string; seconds_remaining: number } | null
): void => {
  if (!navigator.onLine || !isNotificationSupported() || Notification.permission !== 'granted') {
    return;
  }

  // 1. Check Class Alert
  if (nextClass && nextClass.status === 'upcoming') {
    const classId = `${nextClass.subject_code}_${nextClass.start_time}`;
    const notifiedClasses: string[] = JSON.parse(localStorage.getItem(NOTIFIED_CLASSES_KEY) || '[]');

    if (!notifiedClasses.includes(classId)) {
      sendSystemNotification(
        `Upcoming Class: ${nextClass.subject_code}`,
        `${nextClass.subject_title || 'Class'} in Room ${nextClass.room || 'TBA'} (${nextClass.time_badge || nextClass.start_time})`
      );
      notifiedClasses.push(classId);
      localStorage.setItem(NOTIFIED_CLASSES_KEY, JSON.stringify(notifiedClasses.slice(-10)));
    }
  }

  // 2. Check Urgent Assignment Deadline Alert
  if (urgentTask && urgentTask.seconds_remaining <= 24 * 3600 && urgentTask.seconds_remaining > 0) {
    const notifiedTasks: string[] = JSON.parse(localStorage.getItem(NOTIFIED_TASKS_KEY) || '[]');

    if (!notifiedTasks.includes(urgentTask.id)) {
      sendSystemNotification(
        `Urgent Deadline: ${urgentTask.title}`,
        `${urgentTask.course_name} • Due in ${urgentTask.time_left_str}!`
      );
      notifiedTasks.push(urgentTask.id);
      localStorage.setItem(NOTIFIED_TASKS_KEY, JSON.stringify(notifiedTasks.slice(-20)));
    }
  }
};

