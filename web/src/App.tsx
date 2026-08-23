import { useEffect, useRef, useState, lazy, Suspense } from 'react';
import axios from 'axios';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

import { API_BASE_URL } from "@/lib/api";
import { AppLayout } from "@/components/layout/AppLayout";
import { ChatInterface } from "@/components/chat/ChatInterface";
import { AuthKeyModal } from "@/components/auth/AuthKeyModal";
import { injectCss, CSS_STORAGE_KEY } from "@/lib/css-injector";
import { Toaster } from "@/components/ui/sonner";

import { useWebSocket } from "@/hooks/useWebSocket";
import { useTheme, applyThemeWithSchedule } from "@/hooks/useTheme";
import { useIdentity } from "@/hooks/useIdentity";
import {
  INITIAL_AGENT_READINESS,
  normalizeAgentReadiness,
  type AgentReadiness,
} from "@/lib/agent-readiness";

async function fetchAgentReadiness(): Promise<AgentReadiness> {
  const response = await axios.get(`${API_BASE_URL}/api/ready`, {
    validateStatus: (status) => status === 200 || status === 503,
  });
  return normalizeAgentReadiness(response.data);
}

const InstancesList = lazy(() =>
  import("@/components/sessions/SessionsList").then((module) => ({
    default: module.InstancesList,
  }))
);
const ConfigPage = lazy(() =>
  import("@/components/config/ConfigPage").then((module) => ({
    default: module.ConfigPage,
  }))
);
const ChannelsPage = lazy(() =>
  import("@/components/channels/ChannelsPage").then((module) => ({
    default: module.ChannelsPage,
  }))
);
const OverviewPage = lazy(() =>
  import("@/components/overview/OverviewPage").then((module) => ({
    default: module.OverviewPage,
  }))
);
const LogsPage = lazy(() =>
  import("@/components/logs/LogsPage").then((module) => ({
    default: module.LogsPage,
  }))
);
const SkillsPage = lazy(() =>
  import("@/components/skills/SkillsPage").then((module) => ({
    default: module.SkillsPage,
  }))
);
const SubagentsPage = lazy(() =>
  import("@/components/subagents/SubagentsPage").then((module) => ({
    default: module.SubagentsPage,
  }))
);
const AppearancePage = lazy(() =>
  import("@/components/config/AppearancePage").then((module) => ({
    default: module.AppearancePage,
  }))
);
const SetupPage = lazy(() =>
  import("@/components/setup/SetupPage").then((module) => ({
    default: module.SetupPage,
  }))
);
const PersonaPage = lazy(() =>
  import("@/components/persona/PersonaPage").then((module) => ({
    default: module.PersonaPage,
  }))
);
const CronPage = lazy(() =>
  import("@/components/cron/CronPage").then((module) => ({
    default: module.CronPage,
  }))
);
const MemoryPage = lazy(() =>
  import("@/components/memory/MemoryPage").then((module) => ({
    default: module.MemoryPage,
  }))
);
const McpPage = lazy(() =>
  import("./components/config/McpPage").then((module) => ({
    default: module.McpPage,
  }))
);
const VoicePage = lazy(() =>
  import("@/components/voice/VoicePage").then((module) => ({
    default: module.VoicePage,
  }))
);
const TaskQueuePanel = lazy(() =>
  import("./components/queue/TaskQueuePanel").then((module) => ({
    default: module.TaskQueuePanel,
  }))
);
const BrowserSessionsPanel = lazy(() =>
  import("./components/browser/BrowserSessionsPanel").then((module) => ({
    default: module.BrowserSessionsPanel,
  }))
);

const VIEW_META: Record<string, { title: string; description: string }> = {
  chat: { title: "Chat", description: "Live conversation and tool execution." },
  overview: { title: "Overview", description: "System health, gateway access, and controls." },
  queue: { title: "Task Queue", description: "Active work, delivery queue, and scheduled jobs." },
  memory: { title: "Memory", description: "Stored facts, recall mode, and memory management." },
  channels: { title: "Channels", description: "Discord, WhatsApp, and channel configuration." },
  logs: { title: "System Logs", description: "Live backend output and operational events." },
  instances: { title: "Instances", description: "Active sessions, sub-agents, and context state." },
  cron: { title: "Cron Jobs", description: "Scheduled automations and recurring tasks." },
  skills: { title: "Skills", description: "Installed capabilities and tool bundles." },
  subagents: { title: "Subagents", description: "Specialized helper profiles Claude-style, kept lightweight." },
  browsers: { title: "Browser Sessions", description: "Active headless browsers and session modes." },
  mcp: { title: "MCP", description: "External Model Context Protocol servers." },
  voice: { title: "ElevenLabs Voice", description: "List custom voices, customize TTS parameters, and test audio previews." },
  persona: { title: "Persona", description: "Identity, style, and adaptive behavior." },
  appearance: { title: "Appearance", description: "Themes, wallpaper, and visual settings." },
  config: { title: "Configuration", description: "Model, environment, and browser settings." },
};

type ShellRuntimeStatus = {
  isConnected: boolean;
  autonomousMode: boolean;
  pendingApprovals: number;
};

function App() {
  const setupRouteRequested = window.location.pathname === '/setup';
  const [currentView, setCurrentView] = useState('chat');
  const [forceSetup, setForceSetup] = useState(setupRouteRequested);
  const [showAuthModal, setShowAuthModal] = useState(false);
  const [isInitialized, setIsInitialized] = useState(false);
  const [autonomousMode, setAutonomousMode] = useState(false);
  const [rateLimitAlertOpen, setRateLimitAlertOpen] = useState(false);
  const [activity, setActivity] = useState<{ text: string } | null>(null);
  const [personaSetupPending, setPersonaSetupPending] = useState(false);
  const [agentReadiness, setAgentReadiness] = useState<AgentReadiness>(INITIAL_AGENT_READINESS);
  const [readinessPollNonce, setReadinessPollNonce] = useState(0);
  const setupKickoffSessionRef = useRef<string | null>(null);

  const { botIdentity, setBotIdentity, refreshIdentity, lastExplicitFetch } = useIdentity();
  const { handleThemeChange, handleTimeThemeSettingsChange } = useTheme();

  const {
    messages,
    inputValue,
    setInputValue,
    isConnected,
    isTyping,
    sessionId,
    connectWebSocket,
    handleSendMessage,
    editMessage,
    handleNewChat,
  } = useWebSocket({
    onIdentityUpdated: refreshIdentity,
    onRateLimit: () => setRateLimitAlertOpen(true),
    onActivity: (text) => setActivity({ text }),
    onActivityClear: () => setActivity(null),
  });

  const pendingApprovals = messages.filter(
    (message) =>
      message.type === 'tool' &&
      (
        message.toolExecution?.status === 'waiting_confirmation' ||
        message.toolExecution?.status === 'pending_confirmation'
      )
  ).length;
  const currentViewMeta = VIEW_META[currentView] || VIEW_META.chat;
  const shellRuntimeStatus: ShellRuntimeStatus = {
    isConnected,
    autonomousMode,
    pendingApprovals,
  };

  const handleAuthSuccess = (key: string) => {
    localStorage.setItem('limebot_api_key', key);
    axios.defaults.headers.common['X-API-Key'] = key;
    setShowAuthModal(false);
    window.location.reload();
  };

  useEffect(() => {
    document.documentElement.classList.add('dark');

    const savedTheme = localStorage.getItem('limebot-theme') || 'lime';
    applyThemeWithSchedule(savedTheme);

    const reapplyScheduledTheme = () => {
      const baseTheme = localStorage.getItem('limebot-theme') || 'lime';
      applyThemeWithSchedule(baseTheme);
    };
    const themeTimer = window.setInterval(reapplyScheduledTheme, 60_000);
    document.addEventListener('visibilitychange', reapplyScheduledTheme);

    const apiKey = localStorage.getItem('limebot_api_key');
    if (apiKey) {
      axios.defaults.headers.common['X-API-Key'] = apiKey;
    }

    const interceptor = axios.interceptors.response.use(
      response => response,
      error => {
        if (error.response?.status === 401 || error.response?.status === 403) {
          setShowAuthModal(true);
        }
        return Promise.reject(error);
      }
    );

    const customCss = localStorage.getItem(CSS_STORAGE_KEY) || '';
    injectCss(customCss);

    const init = async () => {
      const MAX_RETRIES = 5;
      const RETRY_DELAY = 2000;

      if (setupRouteRequested) {
        setForceSetup(true);
        setPersonaSetupPending(false);
        setIsInitialized(true);
        return;
      }

      for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
        try {
          const statusRes = await axios.get(`${API_BASE_URL}/api/setup/status`);
          if (statusRes.data.configured) {
            setForceSetup(false);
            setPersonaSetupPending(!statusRes.data.persona_ready);
          } else {
            setForceSetup(true);
            setPersonaSetupPending(false);
            setIsInitialized(true);
            return;
          }

          await Promise.all([
            axios.get(`${API_BASE_URL}/api/identity`)
              .then(res => {
                setBotIdentity(res.data);
                lastExplicitFetch.current = Date.now();
              }),
            axios.get(`${API_BASE_URL}/api/config`)
              .then(res => {
                if (res.data.env?.AUTONOMOUS_MODE === 'true') {
                  setAutonomousMode(true);
                }
              }),
            fetchAgentReadiness().then(setAgentReadiness),
          ]);

          break;
        } catch (err: unknown) {
          const status = (err as { response?: { status?: number } })?.response?.status;
          if (status === 401 || status === 403) break;

          if (!(err as { response?: unknown })?.response && attempt < MAX_RETRIES) {
            console.log(`Backend not reachable, retrying in ${RETRY_DELAY}ms (${attempt + 1}/${MAX_RETRIES})...`);
            await new Promise(r => setTimeout(r, RETRY_DELAY));
            continue;
          }
          console.error('Initialization failed:', err);
        }
      }
      setIsInitialized(true);
    };

    void init();
    if (!setupRouteRequested) {
      connectWebSocket();
    }

    return () => {
      axios.interceptors.response.eject(interceptor);
      window.clearInterval(themeTimer);
      document.removeEventListener('visibilitychange', reapplyScheduledTheme);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!isInitialized || forceSetup) return;
    let cancelled = false;
    let timer: number | null = null;
    let attempts = 0;

    const poll = async () => {
      attempts += 1;
      try {
        const next = await fetchAgentReadiness();
        if (cancelled) return;
        setAgentReadiness(next);
        if (next.ready || next.status === 'failed') return;
      } catch {
        if (cancelled) return;
      }

      if (attempts >= 60) {
        setAgentReadiness((current) => ({ ...current, status: 'timeout' }));
        return;
      }
      timer = window.setTimeout(() => void poll(), 500);
    };

    timer = window.setTimeout(() => void poll(), 500);

    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [forceSetup, isInitialized, readinessPollNonce]);

  useEffect(() => {
    if (!isInitialized || forceSetup || !personaSetupPending || !isConnected || !agentReadiness.ready) return;
    if (messages.length > 0 || setupKickoffSessionRef.current === sessionId) return;

    setupKickoffSessionRef.current = sessionId;
    handleSendMessage(
      'Please begin the first-time persona setup interview now.',
      null,
      { echoUserMessage: false }
    );
  }, [
    forceSetup,
    agentReadiness.ready,
    handleSendMessage,
    isConnected,
    isInitialized,
    messages.length,
    personaSetupPending,
    sessionId,
  ]);

  if (!isInitialized) {
    return (
      <div className="min-h-screen bg-background flex flex-col items-center justify-center p-4">
        <div className="fixed inset-0 bg-[radial-gradient(circle_at_50%_50%,rgba(50,205,50,0.05),transparent_70%)]" />
        <div className="relative space-y-8 text-center animate-in fade-in zoom-in duration-700">
          <div className="inline-block relative">
            <div className="absolute inset-0 bg-primary/20 blur-3xl rounded-full scale-150 animate-pulse" />
            <img src="/limeeThinking.png" alt="LimeBot" className="h-40 w-auto relative drop-shadow-2xl" />
          </div>
          <div className="space-y-3">
            <h1 className="text-4xl font-black tracking-tighter text-foreground drop-shadow-sm">
              LIME<span className="text-primary italic">BOT</span>
            </h1>
            <div className="flex items-center justify-center gap-2">
              <div className="h-1.5 w-1.5 rounded-full bg-primary animate-bounce" />
              <div className="h-1.5 w-1.5 rounded-full bg-primary animate-bounce [animation-delay:0.2s]" />
              <div className="h-1.5 w-1.5 rounded-full bg-primary animate-bounce [animation-delay:0.4s]" />
              <span className="text-xs font-mono text-muted-foreground uppercase tracking-widest ml-1">
                Connecting to backend
              </span>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (forceSetup) {
    return (
      <Suspense fallback={<div className="min-h-screen bg-background" />}>
        <SetupPage />
      </Suspense>
    );
  }

  return (
    <AppLayout
      botIdentity={botIdentity}
      activeView={currentView}
      onNavigate={setCurrentView}
      pageTitle={currentViewMeta.title}
      pageDescription={currentViewMeta.description}
      runtimeStatus={shellRuntimeStatus}
    >
      <AuthKeyModal
        isOpen={showAuthModal}
        onSuccess={handleAuthSuccess}
      />

      <AlertDialog open={rateLimitAlertOpen} onOpenChange={setRateLimitAlertOpen}>
        <AlertDialogContent className="border-red-500/50">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-red-500 flex items-center gap-2">
              <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="lucide lucide-alert-octagon"><polygon points="7.86 2 16.14 2 22 7.86 22 16.14 16.14 22 7.86 22 2 16.14 2 7.86 7.86 2" /></svg>
              Rate Limit Exceeded
            </AlertDialogTitle>
            <AlertDialogDescription className="space-y-2">
              <p>The AI service is currently unavailable due to high usage (Quota Exceeded).</p>
              <div className="bg-muted p-4 rounded-md text-sm font-mono text-muted-foreground">
                Detailed error logs have been output to the terminal.
              </div>
              <p className="text-xs text-muted-foreground">Please wait a few moments before trying again, or check your API billing status.</p>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogAction onClick={() => setRateLimitAlertOpen(false)}>Okay</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Suspense fallback={<div className="h-full min-h-[40vh] bg-card/10 rounded-2xl border border-border/30" />}>
        {
          currentView === 'instances' ? (
            <InstancesList currentSessionId={sessionId} />
          ) : currentView === 'queue' ? (
            <TaskQueuePanel />
          ) : currentView === 'cron' ? (
            <CronPage />
          ) : currentView === 'memory' ? (
            <MemoryPage onNavigate={setCurrentView} />
          ) : currentView === 'overview' ? (
            <OverviewPage />
          ) : currentView === 'channels' ? (
            <ChannelsPage />
          ) : currentView === 'logs' ? (
            <LogsPage />
          ) : currentView === 'skills' ? (
            <SkillsPage />
          ) : currentView === 'subagents' ? (
            <SubagentsPage />
          ) : currentView === 'browsers' ? (
            <BrowserSessionsPanel />
          ) : currentView === 'persona' ? (
            <PersonaPage onNavigate={setCurrentView} />
          ) : currentView === 'appearance' ? (
            <AppearancePage
              onThemeChange={handleThemeChange}
              onTimeThemeSettingsChange={handleTimeThemeSettingsChange}
            />
          ) : currentView === 'mcp' ? (
            <McpPage />
          ) : currentView === 'voice' ? (
            <VoicePage />
          ) : currentView === 'config' ? (
            <ConfigPage />
          ) : (
            <ChatInterface
              messages={messages}
              inputValue={inputValue}
              isConnected={isConnected}
              isTyping={isTyping}
              botIdentity={botIdentity}
              activeChatId={sessionId}
              activityText={activity?.text || null}
              agentReadiness={agentReadiness}
              onInputChange={setInputValue}
              onSendMessage={handleSendMessage}
              onEditMessage={editMessage}
              onReconnect={connectWebSocket}
              onNewChat={handleNewChat}
              onRetryReadiness={() => {
                setAgentReadiness(INITIAL_AGENT_READINESS);
                setReadinessPollNonce((value) => value + 1);
              }}
            />
          )
        }
      </Suspense>
      <Toaster position="top-right" />
    </AppLayout >
  );
}

export default App;
