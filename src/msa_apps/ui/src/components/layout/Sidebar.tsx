import { NavLink } from 'react-router-dom'
import { Search, Images, Users, Activity, Settings, Sun, Moon } from 'lucide-react'
import { useTheme } from '../../lib/theme'

const NAV_ITEMS = [
  { to: '/browse',   label: 'Browse',   Icon: Images   },
  { to: '/search',   label: 'Search',   Icon: Search   },
  { to: '/people',   label: 'People',   Icon: Users    },
  { to: '/indexer',  label: 'Indexer',  Icon: Activity },
]

const UTILITY_ITEMS = [
  { to: '/settings', label: 'Settings', Icon: Settings },
]

export function Sidebar() {
  const { theme, toggleTheme } = useTheme()

  return (
    <nav className="flex flex-col w-56 h-full bg-slate-50 dark:bg-zinc-950 border-r border-slate-200 dark:border-zinc-800 px-2 py-4 gap-1 shrink-0">
      <div className="px-3 pb-4 text-xs font-semibold text-zinc-400 dark:text-zinc-500 uppercase tracking-widest">
        Media Search
      </div>
      {NAV_ITEMS.map(({ to, label, Icon }) => (
        <NavLink
          key={to}
          to={to}
          className={({ isActive }) =>
            `flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors ${
              isActive
                ? 'bg-slate-200 dark:bg-zinc-800 text-zinc-900 dark:text-white'
                : 'text-zinc-500 dark:text-zinc-400 hover:bg-slate-100 dark:hover:bg-zinc-800/50 hover:text-zinc-800 dark:hover:text-zinc-200'
            }`
          }
        >
          <Icon size={16} />
          {label}
        </NavLink>
      ))}

      <div className="mt-auto flex flex-col gap-1">
        {UTILITY_ITEMS.map(({ to, label, Icon }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              `flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors ${
                isActive
                  ? 'bg-slate-200 dark:bg-zinc-800 text-zinc-900 dark:text-white'
                  : 'text-zinc-500 dark:text-zinc-400 hover:bg-slate-100 dark:hover:bg-zinc-800/50 hover:text-zinc-800 dark:hover:text-zinc-200'
              }`
            }
          >
            <Icon size={16} />
            {label}
          </NavLink>
        ))}

        <button
          onClick={toggleTheme}
          title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          className="flex items-center gap-2 px-3 py-2 text-xs text-zinc-400 dark:text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 transition-colors"
        >
          {theme === 'dark' ? <Sun size={14} /> : <Moon size={14} />}
          {theme === 'dark' ? 'Light mode' : 'Dark mode'}
        </button>
      </div>
    </nav>
  )
}
