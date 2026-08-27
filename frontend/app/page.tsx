"use client";

import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  Activity,
  AlertTriangle,
  ArrowDownToLine,
  Bot,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Clipboard,
  CloudOff,
  Database,
  FileCode2,
  Gauge,
  GitCompareArrows,
  Layers3,
  Menu,
  Moon,
  Network,
  Play,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Sun,
  Upload,
  X,
  Zap,
  type LucideIcon,
} from "lucide-react";
import {
  api,
  DEFAULT_API,
  getApiUrl,
  setApiUrl,
  subscribeProgress,
  type AgentMetrics,
  type AuditEntry,
  type CompareItem,
  type CompareResult,
  type Decision,
  type Health,
  type Metrics,
} from "@/lib/api";

type Notice = { type: "error" | "success" | "info"; text: string };
type JobState = "idle" | "starting" | "running" | "done" | "error";

const nav = [
  { id: "reconcile", label: "Reconcile", icon: GitCompareArrows },
  { id: "metrics", label: "Metrics", icon: Gauge },
  { id: "exceptions", label: "Exceptions", icon: AlertTriangle },
  { id: "audit", label: "Audit trail", icon: FileCode2 },
  { id: "about", label: "About", icon: CircleHelp },
];

const safe = (value: unknown, fallback = "—") =>
  value === undefined || value === null || value === "" ? fallback : String(value);
const pct = (value?: number | null) =>
  value === undefined || value === null
    ? "—"
    : `${(value <= 1 ? value * 100 : value).toFixed(1)}%`;
const num = (value?: number | null) =>
  value === undefined || value === null
    ? "—"
    : new Intl.NumberFormat("en-IN").format(value);
const money = (value?: number | null) =>
  value === undefined || value === null
    ? "—"
    : `₹${new Intl.NumberFormat("en-IN", {
        maximumFractionDigits: 2,
      }).format(value)}`;
const json = (value: unknown) =>
  typeof value === "string" ? value : JSON.stringify(value, null, 2) || "—";

function statusTone(status?: string) {
  const value = String(status || "unknown").toUpperCase();
  if (value === "MATCHED") return "success";
  if (value === "UNRESOLVED") return "warning";
  if (value === "UNKNOWN") return "neutral";
  return "danger";
}

export default function Page() {
  const [dark, setDark] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [connected, setConnected] = useState<boolean | null>(null);
  const [health, setHealth] = useState<Health>();
  const [apiUrl, setUrl] = useState(DEFAULT_API);
  const [savedUrl, setSavedUrl] = useState(DEFAULT_API);
  const [seed, setSeed] = useState(42);
  const [total, setTotal] = useState(1000);
  const [investigable, setInvestigable] = useState(true);
  const [useLlm, setUseLlm] = useState(false);
  const [simulate, setSimulate] = useState(true);
  const [agent, setAgent] = useState(true);
  const [jobId, setJobId] = useState("");
  const [jobStatus, setJobStatus] = useState<JobState>("idle");
  const [progress, setProgress] = useState(0);
  const [progressMessage, setProgressMessage] = useState("");
  const [progressLlmCalls, setProgressLlmCalls] = useState<number>();
  const [metrics, setMetrics] = useState<Metrics>();
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [exceptions, setExceptions] = useState<Decision[]>([]);
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [compare, setCompare] = useState<CompareResult>();
  const [notice, setNotice] = useState<Notice>();
  const [busy, setBusy] = useState("");
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const streamStop = useRef<(() => void) | undefined>(undefined);
  const pollStop = useRef<(() => void) | undefined>(undefined);

  // Sync theme + backend URL from localStorage on mount (SSR-safe).
  useEffect(() => {
    const saved = getApiUrl();
    setUrl(saved);
    setSavedUrl(saved);
    try {
      setDark(localStorage.getItem("finance-theme") === "dark");
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    try {
      localStorage.setItem("finance-theme", dark ? "dark" : "light");
    } catch {
      /* ignore */
    }
  }, [dark]);
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(undefined), 4500);
    return () => window.clearTimeout(timer);
  }, [notice]);
  useEffect(
    () => () => {
      streamStop.current?.();
      pollStop.current?.();
    },
    [],
  );

  const csvHref = (name: string) => `${savedUrl}/data/${name}.csv`;
  const auditDownloadHref = jobId ? `${savedUrl}/audit/${jobId}` : "#";

  const checkHealth = useCallback(async () => {
    setBusy("health");
    try {
      const result = await api.health();
      setHealth(result);
      setConnected(true);
      setNotice({ type: "success", text: "Backend is reachable." });
    } catch {
      setHealth(undefined);
      setConnected(false);
      setNotice({
        type: "error",
        text: "Backend unreachable. Check the service URL.",
      });
    } finally {
      setBusy("");
    }
  }, []);

  useEffect(() => {
    void checkHealth();
  }, [checkHealth]);

  const finishJob = useCallback(async (id: string) => {
    try {
      const [result, auditData] = await Promise.all([
        api.results(id),
        api.audit(id),
      ]);
      const all = Array.isArray(result.decisions) ? result.decisions : [];
      setMetrics(result.metrics);
      setDecisions(all);
      setExceptions(
        Array.isArray(result.exceptions)
          ? result.exceptions
          : all.filter((decision) => statusTone(decision.status) !== "success"),
      );
      setAudit(Array.isArray(auditData) ? auditData : []);
      setJobStatus("done");
      setProgress(100);
      setProgressMessage("Reconciliation complete");
      setNotice({
        type: "success",
        text: "Results loaded. Every decision is ready to inspect.",
      });
    } catch {
      setJobStatus("error");
      setNotice({
        type: "error",
        text: "The job finished, but results could not be loaded.",
      });
    }
  }, []);

  const startPolling = useCallback(
    (id: string) => {
      let active = true;
      let timer: number | undefined;
      const tick = async () => {
        if (!active) return;
        try {
          const status = await api.status(id);
          setJobStatus(
            status.status === "done"
              ? "done"
              : status.status === "error"
                ? "error"
                : "running",
          );
          const latest = status.latest;
          if (latest?.processed !== undefined && latest.total) {
            setProgress(Math.round((latest.processed / latest.total) * 100));
          }
          setProgressLlmCalls(latest?.llm_calls);
          setProgressMessage(
            latest?.message ||
              (status.status === "error"
                ? safe(status.error, "Job failed")
                : "Polling job status…"),
          );
          if (status.status === "done") {
            await finishJob(id);
          } else if (status.status !== "error") {
            timer = window.setTimeout(tick, 1200);
          }
        } catch {
          timer = window.setTimeout(tick, 1800);
        }
      };
      void tick();
      pollStop.current = () => {
        active = false;
        if (timer) window.clearTimeout(timer);
      };
    },
    [finishJob],
  );

  const startReconcile = async () => {
    streamStop.current?.();
    pollStop.current?.();
    setBusy("reconcile");
    setMetrics(undefined);
    setDecisions([]);
    setExceptions([]);
    setAudit([]);
    setCompare(undefined);
    setJobId("");
    setJobStatus("starting");
    setProgress(3);
    setProgressLlmCalls(undefined);
    try {
      const response = await api.reconcile({
        use_llm: useLlm,
        simulate,
        use_agent: agent,
      });
      if (!response.job_id) throw new Error("No job id returned");
      setJobId(response.job_id);
      setJobStatus("running");
      setProgressMessage("Connecting to live progress stream…");
      streamStop.current = subscribeProgress(
        response.job_id,
        (event) => {
          if (event.processed !== undefined && event.total) {
            setProgress(Math.round((event.processed / event.total) * 100));
          }
          setProgressLlmCalls(event.llm_calls);
          setProgressMessage(
            event.message ||
              (event.last_txn
                ? `${event.last_txn} → ${safe(event.last_status, "processing")}`
                : safe(event.phase, "Reconciling sources…")),
          );
        },
        (status) => {
          if (status === "error") setJobStatus("error");
          void finishJob(response.job_id);
        },
        () => {
          setProgressMessage("Stream dropped — polling status instead.");
          startPolling(response.job_id);
        },
      );
    } catch (error) {
      setJobStatus("error");
      setNotice({
        type: "error",
        text:
          error instanceof Error
            ? error.message
            : "Could not start reconciliation.",
      });
    } finally {
      setBusy("");
    }
  };

  const generate = async () => {
    setBusy("generate");
    try {
      await api.generate({
        seed: Number(seed),
        total: Number(total),
        inject_investigable: investigable,
      });
      setHealth((previous) => ({ ...previous, data_ready: true }));
      setNotice({
        type: "success",
        text: "Source data generated and ready for reconciliation.",
      });
    } catch {
      setNotice({
        type: "error",
        text: "Data generation failed. Check the backend connection.",
      });
    } finally {
      setBusy("");
    }
  };

  const runCompare = async () => {
    setBusy("compare");
    try {
      setCompare(await api.compare({ simulate, use_llm: useLlm }));
      setNotice({ type: "success", text: "Agent comparison is ready." });
    } catch {
      setNotice({ type: "error", text: "Comparison could not be completed." });
    } finally {
      setBusy("");
    }
  };

  const saveUrl = () => {
    setApiUrl(apiUrl);
    setSavedUrl(apiUrl.trim().replace(/\/$/, ""));
    setConnected(null);
    void checkHealth();
  };
  const jump = (id: string) => {
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth" });
    setMobileOpen(false);
  };
  const filtered = useMemo(
    () =>
      exceptions.filter(
        (decision) =>
          (filter === "all" ||
            String(decision.status).toLowerCase() === filter) &&
          (!query ||
            `${decision.txn_id} ${decision.order_id} ${decision.reason}`
              .toLowerCase()
              .includes(query.toLowerCase())),
      ),
    [exceptions, filter, query],
  );
  const statuses = useMemo(
    () =>
      Array.from(
        new Set(
          exceptions.map((decision) =>
            String(decision.status || "unknown").toLowerCase(),
          ),
        ),
      ),
    [exceptions],
  );
  const metricCards: Array<[string, string, string, LucideIcon, string]> = [
    [
      "Match rate",
      pct(metrics?.match_rate),
      `${num(metrics?.auto_matched)} of ${num(metrics?.total_records)}`,
      CheckCircle2,
      "text-emerald-600",
    ],
    ["Exceptions", num(metrics?.exceptions), "need attention", AlertTriangle, "text-amber-600"],
    [
      "Precision / recall",
      `${pct(metrics?.matched_precision)} / ${pct(metrics?.matched_recall)}`,
      "matched decisions",
      ShieldCheck,
      "text-primary",
    ],
    [
      "Throughput",
      metrics?.throughput_rps == null ? "—" : `${metrics.throughput_rps.toFixed(1)} r/s`,
      metrics?.wall_clock_seconds == null
        ? "wall clock unavailable"
        : `${metrics.wall_clock_seconds.toFixed(2)}s wall clock`,
      Zap,
      "text-sky-600",
    ],
  ];

  return (
    <div className="min-h-[100dvh] bg-background text-foreground">
      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-[244px] flex-col border-r border-sidebar-border bg-sidebar text-sidebar-foreground transition-transform duration-300 md:translate-x-0 ${
          mobileOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex h-[76px] items-center gap-3 border-b border-sidebar-border px-6">
          <div className="grid h-9 w-9 place-items-center rounded-lg bg-sidebar-primary text-sidebar-primary-foreground">
            <Network size={19} strokeWidth={2.5} />
          </div>
          <div>
            <div className="text-sm font-extrabold tracking-tight text-sidebar-accent-foreground">
              AI Finance
            </div>
            <div className="mono text-[9px] uppercase tracking-[.18em] text-sidebar-foreground/55">
              Controller
            </div>
          </div>
          <button
            aria-label="Close navigation"
            onClick={() => setMobileOpen(false)}
            className="ml-auto md:hidden"
          >
            <X size={18} />
          </button>
        </div>
        <div className="px-4 pt-7">
          <div className="mono mb-3 px-3 text-[9px] uppercase tracking-[.2em] text-sidebar-foreground/45">
            Workspace
          </div>
          {nav.map(({ id, label, icon: Icon }, index) => (
            <button
              key={id}
              onClick={() => jump(id)}
              className={`group flex w-full items-center gap-3 rounded-lg px-3 py-3 text-left text-[12px] font-semibold transition-colors ${
                index === 0
                  ? "bg-sidebar-accent text-sidebar-accent-foreground"
                  : "text-sidebar-foreground/72 hover:bg-sidebar-accent/70 hover:text-sidebar-accent-foreground"
              }`}
            >
              <Icon size={16} className={index === 0 ? "text-sidebar-primary" : "opacity-70"} />
              <span>{label}</span>
              {id === "exceptions" && exceptions.length > 0 ? (
                <span className="mono ml-auto rounded bg-destructive/15 px-1.5 py-0.5 text-[9px] text-[#ffaaa4]">
                  {exceptions.length}
                </span>
              ) : null}
            </button>
          ))}
        </div>
        <div className="mt-auto border-t border-sidebar-border p-5">
          <div className="mb-3 flex items-center gap-2 text-[11px]">
            <span
              className={`live-dot h-2 w-2 rounded-full ${
                connected === true
                  ? "bg-emerald-400"
                  : connected === false
                    ? "bg-red-400"
                    : "bg-amber-300"
              }`}
            />
            <span className="text-sidebar-foreground/70">
              {connected === true
                ? "Backend connected"
                : connected === false
                  ? "Offline mode"
                  : "Checking connection"}
            </span>
          </div>
          <button
            onClick={() => jump("about")}
            className="flex items-center gap-2 text-[11px] text-sidebar-foreground/48 hover:text-sidebar-accent-foreground"
          >
            <Settings2 size={14} /> Connection settings
          </button>
        </div>
      </aside>
      {mobileOpen ? (
        <button
          aria-label="Close navigation overlay"
          onClick={() => setMobileOpen(false)}
          className="fixed inset-0 z-30 bg-sidebar/40 md:hidden"
        />
      ) : null}
      <main className="md:pl-[244px]">
        <header className="sticky top-0 z-20 flex min-h-[76px] items-center justify-between gap-4 border-b border-border bg-background/90 px-5 py-3 backdrop-blur-md md:px-10">
          <div className="flex items-center gap-3">
            <button
              aria-label="Open navigation"
              onClick={() => setMobileOpen(true)}
              className="rounded-md p-2 hover:bg-muted md:hidden"
            >
              <Menu size={20} />
            </button>
            <div>
              <div className="mono text-[9px] uppercase tracking-[.18em] text-muted-foreground">
                Operations console / India
              </div>
              <h1 className="mt-0.5 text-[17px] font-extrabold tracking-[-.03em]">
                Reconciliation control room
              </h1>
            </div>
          </div>
          <div className="flex items-center gap-1.5">
            <div className="hidden items-center gap-2 rounded-full border border-border bg-card px-3 py-1.5 text-[10px] text-muted-foreground xl:flex">
              <span className="max-w-[190px] truncate">{savedUrl}</span>
              <input
                aria-label="Backend URL"
                value={apiUrl}
                onChange={(event) => setUrl(event.target.value)}
                className="w-[190px] border-l border-border bg-transparent pl-2 text-[10px] outline-none"
              />
              <button onClick={saveUrl} className="font-bold text-primary hover:underline">
                Check
              </button>
            </div>
            <div className="flex items-center gap-2 rounded-full border border-border bg-card px-3 py-1.5 text-[10px] text-muted-foreground">
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  connected ? "bg-emerald-500" : connected === false ? "bg-red-500" : "bg-amber-400"
                }`}
              />
              {connected ? "Live" : connected === false ? "Unreachable" : "Connecting"}
            </div>
            <button
              aria-label="Check backend health"
              onClick={() => void checkHealth()}
              className="rounded-md p-2 text-muted-foreground hover:bg-muted hover:text-foreground"
              title="Check backend"
            >
              <RefreshCw size={16} className={busy === "health" ? "animate-spin" : ""} />
            </button>
            <button
              aria-label="Toggle theme"
              onClick={() => setDark(!dark)}
              className="rounded-md p-2 text-muted-foreground hover:bg-muted hover:text-foreground"
            >
              {dark ? <Sun size={16} /> : <Moon size={16} />}
            </button>
            <div className="ml-1 grid h-8 w-8 place-items-center rounded-full bg-primary/10 text-[11px] font-bold text-primary">
              FC
            </div>
          </div>
        </header>
        <div className="mx-auto max-w-[1500px] px-5 pb-20 md:px-10">
          <section
            id="reconcile"
            className="grid-paper reveal relative mt-7 overflow-hidden rounded-2xl border border-border bg-card p-6 shadow-sm md:p-9"
          >
            <div className="absolute -right-16 -top-20 h-60 w-60 rounded-full bg-primary/8 blur-3xl" />
            <div className="relative grid gap-8 lg:grid-cols-[1fr_390px]">
              <div>
                <div className="mb-5 flex items-center gap-2 text-[10px] font-bold uppercase tracking-[.18em] text-primary">
                  <span className="h-1.5 w-1.5 rounded-full bg-primary" /> Run a controlled pass
                </div>
                <h2 className="max-w-[650px] text-3xl font-extrabold leading-[1.08] tracking-[-.055em] md:text-[43px]">
                  Make every rupee
                  <br />
                  <span className="text-primary">accountable.</span>
                </h2>
                <p className="mt-5 max-w-[560px] text-sm leading-6 text-muted-foreground">
                  Bring orders, settlements, and bank movements into one explainable decision
                  layer. Start with a generated dataset or point the controller at your source
                  files.
                </p>
                <div className="mt-8 flex flex-wrap gap-3">
                  <button
                    onClick={() => void startReconcile()}
                    disabled={!!busy || !connected}
                    className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2.5 text-xs font-bold text-primary-foreground shadow-sm transition-transform hover:-translate-y-0.5 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <Play size={14} fill="currentColor" />
                    {busy === "reconcile" ? "Starting…" : "Run reconciliation"}
                  </button>
                  <button
                    onClick={() => void runCompare()}
                    disabled={!!busy || !connected}
                    className="flex items-center gap-2 rounded-lg border border-border bg-background px-4 py-2.5 text-xs font-bold hover:bg-muted disabled:opacity-50"
                  >
                    <GitCompareArrows size={14} />
                    {busy === "compare" ? "Comparing…" : "A/B: single-shot vs agent"}
                  </button>
                </div>
              </div>
              <RunConfiguration
                seed={seed}
                total={total}
                investigable={investigable}
                useLlm={useLlm}
                agent={agent}
                simulate={simulate}
                onSeed={setSeed}
                onTotal={setTotal}
                onInvestigable={setInvestigable}
                onUseLlm={setUseLlm}
                onAgent={setAgent}
                onSimulate={setSimulate}
              />
            </div>
          </section>

          {jobStatus !== "idle" ? (
            <section className="reveal mt-5 rounded-xl border border-primary/25 bg-primary/5 p-4">
              <div className="flex items-center justify-between gap-4">
                <div className="flex min-w-0 items-center gap-3">
                  <div
                    className={`grid h-9 w-9 shrink-0 place-items-center rounded-lg ${
                      jobStatus === "done"
                        ? "bg-emerald-500/12 text-emerald-600"
                        : jobStatus === "error"
                          ? "bg-destructive/12 text-destructive"
                          : "bg-primary/12 text-primary"
                    }`}
                  >
                    {jobStatus === "done" ? (
                      <CheckCircle2 size={18} />
                    ) : jobStatus === "error" ? (
                      <AlertTriangle size={18} />
                    ) : (
                      <Activity size={18} />
                    )}
                  </div>
                  <div className="min-w-0">
                    <div className="text-xs font-bold">
                      {jobStatus === "done"
                        ? "Pass complete"
                        : jobStatus === "error"
                          ? "Pass stopped"
                          : "Reconciliation in progress"}
                    </div>
                    <div className="truncate text-[11px] text-muted-foreground">
                      {progressMessage || "Preparing job…"}
                      {jobId ? (
                        <span className="mono ml-2 text-[9px] opacity-60">
                          #{jobId.slice(0, 10)}
                        </span>
                      ) : null}
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  {progressLlmCalls !== undefined ? (
                    <span className="mono hidden text-[10px] text-muted-foreground sm:inline">
                      {num(progressLlmCalls)} LLM calls
                    </span>
                  ) : null}
                  <span className="mono text-sm font-medium text-primary">
                    {Math.round(progress)}%
                  </span>
                </div>
              </div>
              <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-primary/10">
                <div
                  className="h-full rounded-full bg-primary transition-[width] duration-500"
                  style={{ width: `${Math.max(2, progress)}%` }}
                />
              </div>
            </section>
          ) : null}

          <section id="metrics" className="mt-10">
            <SectionHeading
              eyebrow="Signal overview"
              title="Metrics that hold up under scrutiny"
              action={
                metrics ? (
                  <span className="mono text-[10px] text-muted-foreground">
                    {safe(metrics.mode, "latest run")} {metrics.agent ? "· agent" : ""}
                  </span>
                ) : undefined
              }
            />
            {metrics ? (
              <>
                <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                  {metricCards.map(([label, value, sub, Icon, color]) => (
                    <MetricCard
                      key={label}
                      label={label}
                      value={value}
                      sub={sub}
                      icon={Icon}
                      color={color}
                    />
                  ))}
                </div>
                <div className="mt-3 grid gap-3 lg:grid-cols-[1.25fr_.75fr]">
                  <QualityBreakdown metrics={metrics} />
                  {metrics.agent ? (
                    <AgentPanel agent={metrics.agent} />
                  ) : (
                    <EmptyState
                      icon={Bot}
                      title="Agent layer not used"
                      text="Enable the investigative agent to see tool calls, verdicts, and guardrail outcomes."
                    />
                  )}
                </div>
              </>
            ) : (
              <EmptyState
                icon={Database}
                title="No run loaded"
                text="Run a reconciliation pass to populate decision quality and operating metrics."
              />
            )}
          </section>

          {compare ? <ComparePanel compare={compare} /> : null}

          <section id="exceptions" className="mt-12">
            <SectionHeading
              eyebrow="Decision queue"
              title="Exceptions, without the noise"
              action={
                exceptions.length > 0 ? (
                  <span className="mono text-[10px] text-muted-foreground">
                    {filtered.length} of {exceptions.length}
                  </span>
                ) : undefined
              }
            />
            {exceptions.length > 0 ? (
              <ExceptionTable
                rows={filtered}
                allRows={exceptions}
                statuses={statuses}
                filter={filter}
                query={query}
                expanded={expanded}
                onFilter={setFilter}
                onQuery={setQuery}
                onExpand={setExpanded}
              />
            ) : (
              <EmptyState
                icon={AlertTriangle}
                title="Exception queue is clear"
                text="Once a run completes, investigated and unmatched records will appear here."
              />
            )}
          </section>

          <section id="audit" className="mt-12">
            <SectionHeading
              eyebrow="Explainability"
              title="Audit trail"
              action={
                audit.length > 0 && jobId ? (
                  <a
                    href={auditDownloadHref}
                    download="audit.json"
                    className="flex items-center gap-1.5 text-[10px] font-bold text-primary hover:underline"
                  >
                    <ArrowDownToLine size={13} /> Download audit JSON
                  </a>
                ) : undefined
              }
            />
            {audit.length > 0 ? (
              <div className="space-y-2">
                {audit.slice(0, 60).map((entry, index) => {
                  const key = String(entry.txn_id || entry.order_id || index);
                  const isOpen = expanded === `audit-${key}`;
                  return (
                    <AuditCard
                      key={key}
                      entry={entry}
                      isOpen={isOpen}
                      onOpen={() => setExpanded(isOpen ? null : `audit-${key}`)}
                      onCopy={() => {
                        void navigator.clipboard?.writeText(json(entry));
                        setNotice({ type: "success", text: "Audit JSON copied." });
                      }}
                    />
                  );
                })}
              </div>
            ) : (
              <EmptyState
                icon={FileCode2}
                title="No audit events yet"
                text="The controller will preserve inputs, layers, ambiguity, and final decision for every loaded run."
              />
            )}
          </section>

          <section id="about" className="mt-12 grid gap-4 lg:grid-cols-[1fr_1.3fr]">
            <div className="rounded-xl border border-border bg-sidebar p-6 text-sidebar-foreground">
              <div className="grid h-9 w-9 place-items-center rounded-lg bg-sidebar-primary text-sidebar-primary-foreground">
                <Network size={18} />
              </div>
              <h3 className="mt-6 text-xl font-extrabold tracking-[-.04em] text-sidebar-accent-foreground">
                A calm place for
                <br />
                messy money.
              </h3>
              <p className="mt-4 text-xs leading-5 text-sidebar-foreground/65">
                AI Finance Controller keeps operators close to the evidence. No black boxes, no
                silent exceptions — just a trace from source input to decision.
              </p>
              <div className="mt-8 flex items-center gap-2 text-[10px] text-sidebar-foreground/50">
                <Layers3 size={13} /> Multi-source reconciliation console
              </div>
            </div>
            <div className="rounded-xl border border-border bg-card p-6">
              <div className="flex items-center justify-between">
                <div>
                  <div className="mono text-[10px] uppercase tracking-[.15em] text-muted-foreground">
                    Connection
                  </div>
                  <h3 className="mt-1 text-sm font-extrabold">Service endpoint</h3>
                </div>
                <CloudOff size={17} className="text-muted-foreground" />
              </div>
              <p className="mt-3 text-xs leading-5 text-muted-foreground">
                The browser connects directly to your FastAPI service. Endpoint preference is
                saved locally on this device.
              </p>
              <div className="mt-5 flex gap-2">
                <input
                  aria-label="Service endpoint URL"
                  value={apiUrl}
                  onChange={(event) => setUrl(event.target.value)}
                  className="min-w-0 flex-1 rounded-md border border-input bg-background px-3 py-2 text-xs outline-none focus:ring-2 focus:ring-ring/30"
                />
                <button
                  onClick={saveUrl}
                  className="rounded-md bg-primary px-3 py-2 text-[10px] font-bold text-primary-foreground"
                >
                  Save & check
                </button>
              </div>
              <div className="mt-5 flex flex-wrap gap-2">
                <DownloadLink label="Ground truth CSV" href={csvHref("ground_truth")} icon={Upload} />
                <DownloadLink
                  label="Settlements CSV"
                  href={csvHref("gateway_settlements")}
                  icon={ArrowDownToLine}
                />
                <DownloadLink
                  label="Order ledger CSV"
                  href={csvHref("order_ledger")}
                  icon={ArrowDownToLine}
                />
                <button
                  onClick={() => void generate()}
                  disabled={!!busy || !connected}
                  className="flex items-center gap-1.5 rounded-md border border-primary/30 px-3 py-2 text-[10px] font-bold text-primary hover:bg-primary/5 disabled:opacity-50"
                >
                  <Database size={13} />
                  {busy === "generate" ? "Generating…" : "Generate data"}
                </button>
              </div>
              {health ? (
                <div className="mt-5 grid gap-2 border-t border-border pt-4 text-[10px] text-muted-foreground sm:grid-cols-3">
                  <span>
                    <b className="text-foreground">Model</b>
                    <br />
                    {safe(health.groq_model, "Not configured")}
                  </span>
                  <span>
                    <b className="text-foreground">Data</b>
                    <br />
                    {health.data_ready ? "Ready" : "Not generated"}
                  </span>
                  <span>
                    <b className="text-foreground">Threshold</b>
                    <br />
                    {pct(health.confidence_threshold)}
                  </span>
                </div>
              ) : null}
            </div>
          </section>
          <footer className="mt-12 flex flex-col justify-between gap-2 border-t border-border py-6 text-[10px] text-muted-foreground sm:flex-row">
            <span>AI Finance Controller · explainable by design</span>
            <span className="mono">API {savedUrl}</span>
          </footer>
        </div>
      </main>
      {notice ? (
        <div
          role="status"
          className={`fixed bottom-5 right-5 z-50 flex max-w-sm items-start gap-3 rounded-lg border bg-card px-4 py-3 text-xs shadow-lg ${
            notice.type === "error" ? "border-destructive/30" : "border-border"
          }`}
        >
          <div
            className={`mt-0.5 h-2 w-2 rounded-full ${
              notice.type === "error"
                ? "bg-destructive"
                : notice.type === "success"
                  ? "bg-emerald-500"
                  : "bg-primary"
            }`}
          />
          <span className="flex-1">{notice.text}</span>
          <button aria-label="Dismiss notification" onClick={() => setNotice(undefined)}>
            <X size={14} className="text-muted-foreground" />
          </button>
        </div>
      ) : null}
    </div>
  );
}

function RunConfiguration({
  seed,
  total,
  investigable,
  useLlm,
  agent,
  simulate,
  onSeed,
  onTotal,
  onInvestigable,
  onUseLlm,
  onAgent,
  onSimulate,
}: {
  seed: number;
  total: number;
  investigable: boolean;
  useLlm: boolean;
  agent: boolean;
  simulate: boolean;
  onSeed: (value: number) => void;
  onTotal: (value: number) => void;
  onInvestigable: (value: boolean) => void;
  onUseLlm: (value: boolean) => void;
  onAgent: (value: boolean) => void;
  onSimulate: (value: boolean) => void;
}) {
  return (
    <div className="rounded-xl border border-border bg-background/75 p-5">
      <div className="mb-4 flex items-center justify-between">
        <div className="mono text-[10px] uppercase tracking-[.15em] text-muted-foreground">
          Run configuration
        </div>
        <SlidersHorizontal size={15} className="text-primary" />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <label className="text-[10px] font-bold text-muted-foreground">
          SEED
          <input
            type="number"
            value={seed}
            onChange={(event) => onSeed(Number(event.target.value))}
            className="mt-1 w-full rounded-md border border-input bg-card px-2.5 py-2 text-xs font-semibold outline-none focus:ring-2 focus:ring-ring/30"
          />
        </label>
        <label className="text-[10px] font-bold text-muted-foreground">
          TOTAL RECORDS
          <input
            type="number"
            min="1"
            value={total}
            onChange={(event) => onTotal(Number(event.target.value))}
            className="mt-1 w-full rounded-md border border-input bg-card px-2.5 py-2 text-xs font-semibold outline-none focus:ring-2 focus:ring-ring/30"
          />
        </label>
      </div>
      <div className="mt-4 space-y-2.5">
        <ToggleRow label="Inject investigable cases" checked={investigable} onChange={onInvestigable} />
        <ToggleRow label="Use LLM resolver" checked={useLlm} onChange={onUseLlm} />
        <ToggleRow label="Use investigative agent" checked={agent} onChange={onAgent} icon={Bot} />
        <ToggleRow label="Simulation mode" checked={simulate} onChange={onSimulate} />
      </div>
      <p className="mt-4 border-t border-border pt-3 text-[10px] leading-4 text-muted-foreground">
        Single-shot resolves ambiguity once. The agent layer can inspect evidence with tools
        before the final guarded verdict.
      </p>
    </div>
  );
}

function ToggleRow({
  label,
  checked,
  onChange,
  icon: Icon,
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
  icon?: LucideIcon;
}) {
  return (
    <label className="flex cursor-pointer items-center justify-between text-xs font-semibold">
      <span className="flex items-center gap-2">
        {Icon ? (
          <Icon size={14} className="text-primary" />
        ) : (
          <span className="h-1 w-1 rounded-full bg-muted-foreground" />
        )}
        {label}
      </span>
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="h-4 w-4 accent-[hsl(var(--primary))]"
      />
    </label>
  );
}

function MetricCard({
  label,
  value,
  sub,
  icon: Icon,
  color,
}: {
  label: string;
  value: string;
  sub: string;
  icon: LucideIcon;
  color: string;
}) {
  return (
    <div className="rounded-xl border border-border bg-card p-5 shadow-sm">
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-bold text-muted-foreground">{label}</span>
        <Icon size={16} className={color} />
      </div>
      <div className="mt-4 text-2xl font-extrabold tracking-[-.04em]">{value}</div>
      <div className="mt-1 text-[10px] text-muted-foreground">{sub}</div>
    </div>
  );
}

function QualityBreakdown({ metrics }: { metrics: Metrics }) {
  const bars = [
    ["True positives", metrics.confusion?.tp],
    ["False positives", metrics.confusion?.fp],
    ["False negatives", metrics.confusion?.fn],
    ["True negatives", metrics.confusion?.tn],
  ] as Array<[string, number | undefined]>;
  const max = Math.max(...bars.map(([, value]) => value || 0), 1);
  return (
    <div className="rounded-xl border border-border bg-card p-5 shadow-sm">
      <div className="flex items-center justify-between">
        <div>
          <div className="mono text-[10px] uppercase tracking-[.15em] text-primary">
            Quality matrix
          </div>
          <h3 className="mt-1 text-sm font-extrabold">Decision confidence</h3>
        </div>
        <span className="text-[10px] text-muted-foreground">
          Status accuracy {pct(metrics.status_accuracy)}
        </span>
      </div>
      <div className="mt-5 grid gap-3 sm:grid-cols-2">
        {bars.map(([label, value]) => (
          <div key={label}>
            <div className="mb-1.5 flex justify-between text-[10px] text-muted-foreground">
              <span>{label}</span>
              <span className="mono font-bold text-foreground">{num(value)}</span>
            </div>
            <div className="h-1.5 rounded-full bg-muted">
              <div
                className={`h-full rounded-full ${
                  label.toLowerCase().includes("false") ? "bg-amber-500" : "bg-primary"
                }`}
                style={{ width: `${((value || 0) / max) * 100}%` }}
              />
            </div>
          </div>
        ))}
      </div>
      <div className="mt-5 flex flex-wrap gap-2">
        {Object.entries(metrics.by_status || {}).map(([status, count]) => (
          <span
            key={status}
            className="rounded-full border border-border px-2.5 py-1 text-[10px] font-semibold"
          >
            {status} <span className="mono text-muted-foreground">{count}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

function AgentPanel({ agent }: { agent: AgentMetrics }) {
  const groq = agent.groq;
  return (
    <div className="rounded-xl border border-primary/20 bg-primary/[.035] p-5 shadow-sm">
      <div className="flex items-center justify-between">
        <div>
          <div className="mono text-[10px] uppercase tracking-[.15em] text-primary">
            Investigative agent
          </div>
          <h3 className="mt-1 text-sm font-extrabold">Evidence before verdict</h3>
        </div>
        <Bot size={18} className="text-primary" />
      </div>
      <div className="mt-5 grid grid-cols-2 gap-3">
        <SmallStat label="Records investigated" value={num(agent.records_investigated)} />
        <SmallStat label="Average steps" value={num(agent.avg_steps)} />
        <SmallStat label="Tool calls" value={num(agent.total_tool_calls)} />
        <SmallStat label="Verdicts honored" value={num(agent.verdicts_honored)} />
      </div>
      <div className="mt-4 flex flex-wrap gap-3 border-t border-primary/10 pt-4 text-[10px] text-muted-foreground">
        <span>
          Guardrail overrides{" "}
          <b className="text-foreground">{num(agent.verdicts_overridden_by_guardrail)}</b>
        </span>
        <span>
          Errors <b className="text-foreground">{num(agent.verdicts_errored)}</b>
        </span>
        <span>
          Groq calls <b className="text-foreground">{num(groq?.total_calls)}</b>
        </span>
        <span>
          Peak RPM{" "}
          <b className="text-foreground">
            {groq ? `${num(groq.peak_rpm)} / ${num(groq.rpm_cap)}` : "—"}
          </b>
        </span>
      </div>
    </div>
  );
}

function SmallStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-card/80 p-3">
      <div className="text-[9px] font-bold text-muted-foreground">{label}</div>
      <div className="mt-1.5 text-lg font-extrabold tracking-[-.04em]">{value}</div>
    </div>
  );
}

function ComparePanel({ compare }: { compare: CompareResult }) {
  const rows: Array<[string, (metrics?: Metrics) => string]> = [
    ["Match rate", (metrics) => pct(metrics?.match_rate)],
    ["Recall", (metrics) => pct(metrics?.matched_recall)],
    ["False positives", (metrics) => num(metrics?.false_positive_count)],
    ["Status accuracy", (metrics) => pct(metrics?.status_accuracy)],
  ];
  return (
    <section className="mt-6 rounded-xl border border-border bg-card p-5 shadow-sm">
      <div className="flex items-center justify-between">
        <div>
          <div className="mono text-[10px] uppercase tracking-[.15em] text-primary">
            Agent benchmark
          </div>
          <h3 className="mt-1 text-sm font-extrabold">Single-shot vs. agent layer</h3>
        </div>
        <GitCompareArrows size={18} className="text-muted-foreground" />
      </div>
      <div className="mt-5 overflow-x-auto">
        <table className="w-full min-w-[480px] text-left text-xs">
          <thead className="mono text-[9px] uppercase tracking-[.15em] text-muted-foreground">
            <tr>
              <th className="pb-2">Measure</th>
              <th className="pb-2">Single-shot</th>
              <th className="pb-2">Agent</th>
              <th className="pb-2">Delta</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(([label, read]) => {
              const single = read(compare.single);
              const agentValue = read(compare.agent);
              return (
                <tr key={label} className="border-t border-border">
                  <td className="py-3 font-semibold">{label}</td>
                  <td className="py-3 text-muted-foreground">{single}</td>
                  <td className="py-3 font-bold text-primary">{agentValue}</td>
                  <td className="py-3 text-emerald-600">
                    {single === agentValue ? "—" : "improved"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="mt-5 grid gap-4 lg:grid-cols-2">
        <CompareCallout title="Resolution lift" items={compare.lift || []} tone="success" />
        <CompareCallout
          title="Regressions to review"
          items={compare.regressions || []}
          tone="warning"
        />
      </div>
    </section>
  );
}

function CompareCallout({
  title,
  items,
  tone,
}: {
  title: string;
  items: CompareItem[];
  tone: "success" | "warning";
}) {
  return (
    <div className={`rounded-lg p-4 ${tone === "success" ? "bg-primary/8" : "bg-amber-500/8"}`}>
      <div className="flex items-center justify-between">
        <span className="text-xs font-extrabold">{title}</span>
        <span className="mono text-[10px] text-muted-foreground">{items.length} records</span>
      </div>
      {items.length > 0 ? (
        <div className="mt-3 space-y-2">
          {items.slice(0, 5).map((item, index) => (
            <div
              key={`${item.txn_id}-${index}`}
              className="flex items-center justify-between gap-3 border-t border-current/10 pt-2 text-[10px]"
            >
              <span className="mono font-semibold">{safe(item.txn_id)}</span>
              <span className="text-muted-foreground">
                {safe(item.single)} → <b className="text-foreground">{safe(item.agent)}</b> · truth{" "}
                {safe(item.truth)}
              </span>
            </div>
          ))}
        </div>
      ) : (
        <p className="mt-3 text-[10px] text-muted-foreground">No records in this category.</p>
      )}
    </div>
  );
}

function ExceptionTable({
  rows,
  allRows,
  statuses,
  filter,
  query,
  expanded,
  onFilter,
  onQuery,
  onExpand,
}: {
  rows: Decision[];
  allRows: Decision[];
  statuses: string[];
  filter: string;
  query: string;
  expanded: string | null;
  onFilter: (value: string) => void;
  onQuery: (value: string) => void;
  onExpand: (value: string | null) => void;
}) {
  return (
    <div className="overflow-hidden rounded-xl border border-border bg-card shadow-sm">
      <div className="flex flex-col gap-3 border-b border-border p-4 md:flex-row md:items-center">
        <div className="relative max-w-xs flex-1">
          <Search size={14} className="absolute left-3 top-2.5 text-muted-foreground" />
          <input
            value={query}
            onChange={(event) => onQuery(event.target.value)}
            placeholder="Search transaction or reason"
            className="w-full rounded-md border border-input bg-background py-2 pl-9 pr-3 text-xs outline-none focus:ring-2 focus:ring-ring/30"
          />
        </div>
        <div className="flex gap-1 overflow-x-auto">
          <FilterButton
            label="all"
            count={allRows.length}
            active={filter === "all"}
            onClick={() => onFilter("all")}
          />
          {statuses.map((status) => (
            <FilterButton
              key={status}
              label={status}
              count={allRows.filter((row) => String(row.status).toLowerCase() === status).length}
              active={filter === status}
              onClick={() => onFilter(status)}
            />
          ))}
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-left">
          <thead className="bg-muted/55">
            <tr className="mono text-[9px] uppercase tracking-[.15em] text-muted-foreground">
              <th className="px-5 py-3">Transaction</th>
              <th className="px-3 py-3">Status</th>
              <th className="px-3 py-3">Source</th>
              <th className="px-3 py-3">Confidence</th>
              <th className="px-3 py-3">Δ ₹</th>
              <th className="px-3 py-3">Reason</th>
              <th className="px-3 py-3" />
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, 100).map((decision, index) => {
              const key = String(decision.txn_id || decision.order_id || index);
              const isOpen = expanded === `decision-${key}`;
              return (
                <Fragment key={key}>
                  <tr className="border-t border-border/70 text-xs hover:bg-muted/30">
                    <td className="px-5 py-4">
                      <div className="mono font-medium">{safe(decision.txn_id)}</div>
                      <div className="mt-1 text-[10px] text-muted-foreground">
                        Order {safe(decision.order_id)}
                      </div>
                    </td>
                    <td className="px-3 py-4">
                      <StatusPill status={decision.status} />
                    </td>
                    <td className="px-3 py-4 text-[10px] text-muted-foreground">
                      {safe(decision.source)}
                    </td>
                    <td className="mono px-3 py-4">{pct(decision.confidence)}</td>
                    <td className="mono px-3 py-4">{money(decision.amount_delta)}</td>
                    <td className="max-w-[290px] px-3 py-4 text-muted-foreground">
                      {safe(decision.reason)}
                    </td>
                    <td className="px-3 py-4">
                      <button
                        aria-label={`Expand ${safe(decision.txn_id, "decision")}`}
                        onClick={() => onExpand(isOpen ? null : `decision-${key}`)}
                        className="rounded p-1.5 hover:bg-muted"
                      >
                        {isOpen ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                      </button>
                    </td>
                  </tr>
                  {isOpen ? (
                    <tr>
                      <td colSpan={7} className="bg-primary/5 px-5 py-4">
                        <div className="grid gap-3 text-[11px] md:grid-cols-4">
                          <div>
                            <span className="mono text-[9px] uppercase text-muted-foreground">
                              Source
                            </span>
                            <div className="mt-1 font-semibold">{safe(decision.source)}</div>
                          </div>
                          <div>
                            <span className="mono text-[9px] uppercase text-muted-foreground">
                              Matched UTR
                            </span>
                            <div className="mono mt-1">{safe(decision.matched_utr)}</div>
                          </div>
                          <div>
                            <span className="mono text-[9px] uppercase text-muted-foreground">
                              LLM used
                            </span>
                            <div className="mt-1 font-semibold">
                              {decision.llm_used === undefined
                                ? "—"
                                : decision.llm_used
                                  ? "Yes"
                                  : "No"}
                            </div>
                          </div>
                          <div>
                            <span className="mono text-[9px] uppercase text-muted-foreground">
                              Order ID
                            </span>
                            <div className="mono mt-1">{safe(decision.order_id)}</div>
                          </div>
                        </div>
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
          </tbody>
        </table>
        {rows.length === 0 ? (
          <div className="p-10 text-center text-xs text-muted-foreground">
            No decisions match this filter.
          </div>
        ) : null}
      </div>
    </div>
  );
}

function FilterButton({
  label,
  count,
  active,
  onClick,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`whitespace-nowrap rounded-md px-3 py-1.5 text-[10px] font-bold capitalize ${
        active ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground hover:text-foreground"
      }`}
    >
      {label} <span className="mono ml-1 opacity-70">{count}</span>
    </button>
  );
}

function StatusPill({ status }: { status?: string }) {
  const tone = statusTone(status);
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-1 text-[10px] font-bold ${
        tone === "success"
          ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
          : tone === "warning"
            ? "bg-amber-500/12 text-amber-700 dark:text-amber-300"
            : tone === "danger"
              ? "bg-red-500/10 text-red-700 dark:text-red-300"
              : "bg-muted text-muted-foreground"
      }`}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current" />
      {safe(status, "UNKNOWN")}
    </span>
  );
}

function AuditCard({
  entry,
  isOpen,
  onOpen,
  onCopy,
}: {
  entry: AuditEntry;
  isOpen: boolean;
  onOpen: () => void;
  onCopy: () => void;
}) {
  return (
    <div className="rounded-xl border border-border bg-card shadow-sm">
      <button onClick={onOpen} className="flex w-full items-center gap-4 p-4 text-left">
        <div className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-primary/10 text-primary">
          <FileCode2 size={15} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="mono text-[11px] font-medium">{safe(entry.txn_id || entry.order_id)}</div>
          <div className="mt-1 truncate text-[11px] text-muted-foreground">
            {safe(entry.rule_or_layer)} · {safe(entry.ambiguity_reason, "No ambiguity recorded")}
          </div>
        </div>
        <div className="hidden text-right sm:block">
          <div className="mono text-[10px] text-muted-foreground">{safe(entry.timestamp)}</div>
          <div className="mt-1 text-[10px] font-bold">{safe(entry.decision?.status)}</div>
        </div>
        {isOpen ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
      </button>
      {isOpen ? <AuditDetail entry={entry} onCopy={onCopy} /> : null}
    </div>
  );
}

function AuditDetail({ entry, onCopy }: { entry: AuditEntry; onCopy: () => void }) {
  const trace = entry.llm_trace;
  // The backend stores the investigation trail in `evidence` (an array of step
  // objects), with `agent_steps` / `agent_tool_calls` as numeric counts.
  const evidence = (trace?.evidence || []) as Array<Record<string, any>>;
  const stepCount = trace?.agent_steps;
  const toolCount = trace?.agent_tool_calls;
  const timeline = evidence.map((item) => {
    const type = String(item.type || "evidence");
    if (type === "tool_call")
      return {
        label: "Tool call",
        value: { tool: item.tool, arguments: item.arguments, observation: item.observation },
      };
    if (type === "thought") return { label: "Agent thought", value: item.text };
    if (type === "verdict") return { label: "Verdict", value: item.arguments };
    return { label: "Evidence", value: item };
  });
  return (
    <div className="border-t border-border bg-muted/30 p-4">
      <div className="grid gap-4 md:grid-cols-3">
        <div>
          <div className="mono text-[9px] uppercase tracking-wider text-muted-foreground">
            Rule / layer
          </div>
          <div className="mt-1 text-xs font-semibold">{safe(entry.rule_or_layer)}</div>
        </div>
        <div>
          <div className="mono text-[9px] uppercase tracking-wider text-muted-foreground">
            Ambiguity reason
          </div>
          <div className="mt-1 text-xs">{safe(entry.ambiguity_reason, "None recorded")}</div>
        </div>
        <div>
          <div className="mono text-[9px] uppercase tracking-wider text-muted-foreground">
            Final decision
          </div>
          <div className="mt-1 text-xs font-semibold">{safe(entry.decision?.status)}</div>
        </div>
      </div>
      {trace ? (
        <div className="mt-5 rounded-lg border border-primary/15 bg-primary/[.035] p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-xs font-bold">
              <Bot size={14} className="text-primary" /> Investigation by{" "}
              {safe(trace.model, safe(trace.provider, "resolver"))}
            </div>
            <div className="mono text-[9px] text-muted-foreground">
              {safe(stepCount)} steps · {safe(toolCount)} tool calls · guardrail:{" "}
              {safe(trace.guardrail, "not reported")}
            </div>
          </div>
          {timeline.length > 0 ? (
            <div className="mt-4 space-y-3">
              {timeline.map((item, index) => (
                <div key={`${item.label}-${index}`} className="relative flex gap-3 pl-1">
                  <div className="flex w-4 flex-col items-center">
                    <span className="mt-1.5 h-2 w-2 rounded-full bg-primary" />
                    {index < timeline.length - 1 ? (
                      <span className="h-full w-px bg-primary/20" />
                    ) : null}
                  </div>
                  <div className="min-w-0 pb-1">
                    <div className="mono text-[9px] uppercase tracking-wider text-primary">
                      {item.label}
                    </div>
                    <pre className="mt-1 max-h-24 overflow-auto whitespace-pre-wrap break-words text-[10px] leading-4 text-muted-foreground">
                      {json(item.value)}
                    </pre>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <p className="mt-3 text-[10px] text-muted-foreground">
              No investigation steps were recorded.
            </p>
          )}
          <pre className="mt-4 max-h-44 overflow-auto rounded-md bg-sidebar p-3 text-[10px] leading-4 text-sidebar-foreground">
            {json(trace.raw || trace)}
          </pre>
        </div>
      ) : (
        <div className="mt-5 rounded-lg border border-border bg-card p-3 text-[10px] text-muted-foreground">
          Deterministic resolution — no LLM investigation trace was recorded.
        </div>
      )}
      <div className="mt-4 flex items-center justify-between">
        <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
          <Check size={13} className="text-emerald-600" /> Evidence chain preserved
        </div>
        <button
          onClick={onCopy}
          className="flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1.5 text-[10px] font-bold hover:bg-muted"
        >
          <Clipboard size={13} /> Copy raw JSON
        </button>
      </div>
    </div>
  );
}

function DownloadLink({ label, href, icon: Icon }: { label: string; href: string; icon: LucideIcon }) {
  return (
    <a
      href={href}
      download
      className="flex items-center gap-1.5 rounded-md border border-border px-3 py-2 text-[10px] font-bold hover:bg-muted"
    >
      <Icon size={13} /> {label}
    </a>
  );
}

function SectionHeading({
  eyebrow,
  title,
  action,
}: {
  eyebrow: string;
  title: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-4 flex items-end justify-between gap-4">
      <div>
        <div className="mono text-[10px] uppercase tracking-[.18em] text-primary">{eyebrow}</div>
        <h2 className="mt-1 text-xl font-extrabold tracking-[-.04em]">{title}</h2>
      </div>
      {action}
    </div>
  );
}

function EmptyState({ icon: Icon, title, text }: { icon: LucideIcon; title: string; text: string }) {
  return (
    <div className="grid min-h-[150px] place-items-center rounded-xl border border-dashed border-border bg-card/50 p-6 text-center">
      <div>
        <Icon size={22} className="mx-auto text-muted-foreground/50" />
        <div className="mt-3 text-sm font-bold">{title}</div>
        <p className="mt-1 max-w-sm text-xs leading-5 text-muted-foreground">{text}</p>
      </div>
    </div>
  );
}
