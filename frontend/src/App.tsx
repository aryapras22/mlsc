import { Navigate, Route, Routes } from "react-router-dom";
import { Shell } from "@/pages/Shell";
import { MonitorList } from "@/pages/MonitorList";
import { OverviewPage } from "@/pages/OverviewPage";
import { TopicsPage } from "@/pages/TopicsPage";
import { TopicDetailPage } from "@/pages/TopicDetailPage";
import { TrendsPage } from "@/pages/TrendsPage";
import { IdeasPage } from "@/pages/IdeasPage";
import { ExplorerPage } from "@/pages/ExplorerPage";
import { ComparePage } from "@/pages/ComparePage";

function App() {
  return (
    <Routes>
      <Route path="/" element={<MonitorList />} />
      <Route path="/monitors/:monitorId" element={<Shell />}>
        <Route index element={<Navigate to="overview" replace />} />
        <Route path="overview" element={<OverviewPage />} />
        <Route path="topics" element={<TopicsPage />} />
        <Route path="topics/:topicId" element={<TopicDetailPage />} />
        <Route path="trends" element={<TrendsPage />} />
        <Route path="ideas" element={<IdeasPage />} />
        <Route path="explorer" element={<ExplorerPage />} />
        <Route path="compare" element={<ComparePage />} />
      </Route>
    </Routes>
  );
}

export default App;
