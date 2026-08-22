import { Outlet, useParams, useLocation, Link } from "react-router-dom";

const TABS = [
  { path: "overview", label: "Overview" },
  { path: "topics", label: "Topics" },
  { path: "trends", label: "Trends" },
  { path: "ideas", label: "Ideas" },
  { path: "explorer", label: "Explorer" },
  { path: "compare", label: "Compare" },
  { path: "settings", label: "Settings" },
] as const;

export function Shell() {
  const { monitorId } = useParams<{ monitorId: string }>();
  const location = useLocation();

  return (
    <div className="min-h-screen">
      <header className="border-b p-4">
        <h1 className="text-lg font-semibold">Monitor {monitorId}</h1>
        <nav className="mt-2 flex gap-4">
          {TABS.map((tab) => {
            const href = `/monitors/${monitorId}/${tab.path}${location.search}`;
            const active = location.pathname.endsWith(`/${tab.path}`);
            return (
              <Link
                key={tab.path}
                to={href}
                className={active ? "font-semibold underline" : "text-muted-foreground"}
              >
                {tab.label}
              </Link>
            );
          })}
        </nav>
      </header>
      <main className="p-4">
        <Outlet />
      </main>
    </div>
  );
}
