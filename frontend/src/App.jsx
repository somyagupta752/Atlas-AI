import { NavLink, Route, Routes } from "react-router-dom";
import Overview from "./pages/Overview";
import StartEvaluation from "./pages/StartEvaluation";
import EvaluationDetail from "./pages/EvaluationDetail";
import Compare from "./pages/Compare";
import CaseInvestigation from "./pages/CaseInvestigation";

export default function App() {
  return (
    <>
      <header className="top">
        <strong>Atlas AI · Evaluation Lab</strong>
        <nav>
          <NavLink to="/" end>Overview</NavLink>
          <NavLink to="/start">Start evaluation</NavLink>
          <NavLink to="/compare">Compare</NavLink>
        </nav>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/start" element={<StartEvaluation />} />
          <Route path="/runs/:id" element={<EvaluationDetail />} />
          <Route path="/runs/:id/cases/:caseId" element={<CaseInvestigation />} />
          <Route path="/compare" element={<Compare />} />
          <Route path="*" element={<p className="state">Page not found.</p>} />
        </Routes>
      </main>
    </>
  );
}
