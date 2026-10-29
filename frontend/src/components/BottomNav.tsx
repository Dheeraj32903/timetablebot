import React from 'react';
import { 
  LayoutGrid, 
  CalendarDays, 
  CheckSquare, 
  Ticket, 
  Settings 
} from 'lucide-react';

export type TabType = 'dashboard' | 'timetable' | 'tasks' | 'outpass' | 'settings';

interface BottomNavProps {
  activeTab: TabType;
  onTabChange: (tab: TabType) => void;
  urgentCount?: number;
}

export const BottomNav: React.FC<BottomNavProps> = ({ activeTab, onTabChange, urgentCount = 0 }) => {
  const tabs = [
    { id: 'dashboard' as TabType, label: 'Home', icon: LayoutGrid },
    { id: 'timetable' as TabType, label: 'Timetable', icon: CalendarDays },
    { id: 'tasks' as TabType, label: 'Tasks', icon: CheckSquare, badge: urgentCount > 0 ? urgentCount : undefined },
    { id: 'outpass' as TabType, label: 'Outpass', icon: Ticket },
    { id: 'settings' as TabType, label: 'Settings', icon: Settings },
  ];

  const handleSelect = (id: TabType) => {
    if (window.Telegram?.WebApp?.HapticFeedback) {
      window.Telegram.WebApp.HapticFeedback.impactOccurred('light');
    } else if (navigator?.vibrate) {
      navigator.vibrate(8);
    }
    onTabChange(id);
  };

  return (
    <div className="fixed bottom-0 left-0 right-0 z-50 bg-[#080B14]/95 backdrop-blur-xl border-t border-white/5">
      <nav className="max-w-md mx-auto px-2 py-2 flex items-center justify-around">
        {tabs.map((tab) => {
          const Icon = tab.icon;
          const isActive = activeTab === tab.id;
          return (
            <button
              key={tab.id}
              onClick={() => handleSelect(tab.id)}
              className={`flex flex-col items-center justify-center py-1 flex-1 transition-all ${
                isActive ? 'text-white' : 'text-slate-400 hover:text-slate-300'
              }`}
            >
              <div className="relative">
                <Icon
                  size={20}
                  strokeWidth={isActive ? 2.2 : 1.75}
                  className={isActive ? 'text-[#8B5CF6]' : 'text-slate-400'}
                />
                {tab.badge !== undefined && (
                  <span className="absolute -top-1.5 -right-2 min-w-[15px] h-3.5 px-1 rounded-full bg-rose-500 text-white text-[9px] font-bold flex items-center justify-center shadow-sm">
                    {tab.badge}
                  </span>
                )}
              </div>

              <span className={`text-[10px] mt-1 tracking-tight ${
                isActive ? 'text-[#8B5CF6] font-bold' : 'text-slate-400 font-medium'
              }`}>
                {tab.label}
              </span>

              {isActive ? (
                <div className="w-6 h-[2px] bg-[#8B5CF6] rounded-full mt-1" />
              ) : (
                <div className="w-6 h-[2px] bg-transparent mt-1" />
              )}
            </button>
          );
        })}
      </nav>
    </div>
  );
};
