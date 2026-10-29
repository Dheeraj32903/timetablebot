import React, { useState, useEffect } from 'react';
import { 
  DashboardResponse, 
  UserProfile, 
  fetchDashboard, 
  fetchProfile 
} from './api/client';
import { Header } from './components/Header';
import { BottomNav, TabType } from './components/BottomNav';
import { HomeDashboard } from './screens/HomeDashboard';
import { TimetableScreen } from './screens/TimetableScreen';
import { AssignmentsScreen } from './screens/AssignmentsScreen';
import { OutpassScreen } from './screens/OutpassScreen';
import { SettingsScreen } from './screens/SettingsScreen';

export const App: React.FC = () => {
  const [activeTab, setActiveTab] = useState<TabType>('dashboard');
  const [dashboardData, setDashboardData] = useState<DashboardResponse | null>(null);
  const [userProfile, setUserProfile] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshing, setRefreshing] = useState<boolean>(false);

  // Initialize Telegram WebApp settings
  useEffect(() => {
    if (typeof window !== 'undefined' && window.Telegram?.WebApp) {
      const tg = window.Telegram.WebApp;
      try {
        tg.ready();
        tg.expand();
        tg.setHeaderColor('#080B14');
        tg.setBackgroundColor('#080B14');
      } catch (err) {
        console.warn('Telegram WebApp initialization error:', err);
      }
    }
  }, []);

  const loadInitialData = async () => {
    setLoading(true);
    try {
      const [dash, profile] = await Promise.allSettled([
        fetchDashboard(),
        fetchProfile(),
      ]);

      if (dash.status === 'fulfilled') {
        setDashboardData(dash.value);
        setUserProfile(dash.value.user);
      } else if (profile.status === 'fulfilled') {
        setUserProfile(profile.value);
      }
    } catch (err) {
      console.error('Failed to load initial data:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      const dash = await fetchDashboard();
      setDashboardData(dash);
      setUserProfile(dash.user);
      if (window.Telegram?.WebApp?.HapticFeedback) {
        window.Telegram.WebApp.HapticFeedback.notificationOccurred('success');
      } else if (navigator?.vibrate) {
        navigator.vibrate([15, 50, 15]);
      }
    } catch (err) {
      console.error('Refresh error:', err);
    } finally {
      setRefreshing(false);
    }
  };

  useEffect(() => {
    loadInitialData();
  }, []);

  return (
    <div className="relative min-h-screen bg-[#080B14] text-slate-100 flex flex-col font-sans selection:bg-brand-violet selection:text-white overflow-hidden">
      {/* Dynamic Ambient Background Glows */}
      <div className="ambient-glow-violet" />
      <div className="ambient-glow-indigo" />

      {/* Main Responsive Android App Container */}
      <div className="relative z-10 flex-1 flex flex-col w-full max-w-md mx-auto bg-[#080B14]/40 backdrop-blur-3xl shadow-2xl min-h-screen sm:border-x sm:border-white/[0.05]">
        {/* Top App Bar Header */}
        <Header 
          user={userProfile} 
          onRefresh={handleRefresh} 
          isRefreshing={refreshing} 
        />

        {/* Dynamic Screen View */}
        <main className="flex-1 w-full overflow-y-auto">
          {activeTab === 'dashboard' && (
            <HomeDashboard 
              data={dashboardData} 
              loading={loading} 
              onNavigate={setActiveTab} 
            />
          )}
          {activeTab === 'timetable' && <TimetableScreen />}
          {activeTab === 'tasks' && <AssignmentsScreen />}
          {activeTab === 'outpass' && <OutpassScreen />}
          {activeTab === 'settings' && (
            <SettingsScreen 
              user={userProfile} 
              onUserUpdate={setUserProfile} 
            />
          )}
        </main>

        {/* Floating Android Bottom Navigation Dock */}
        <BottomNav 
          activeTab={activeTab} 
          onTabChange={setActiveTab} 
          urgentCount={dashboardData?.urgent_tasks_count ?? 0} 
        />
      </div>
    </div>
  );
};

export default App;
