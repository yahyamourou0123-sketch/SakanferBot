import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Send, Palette, Plus, Trash2, Bell, PhoneOff, Mic, Square,
  Menu, X, Paperclip, FileText, Image as ImageIcon, Copy, Check,
  AudioLines, AlertCircle, Search, Download, Loader2
} from 'lucide-react';
import { GoogleGenAI, Modality } from '@google/genai';
import {
  streamChat, generateImageViaApi, transcribeViaApi,
  getLiveApiKey, getChatoPrompt, generateTitle
} from './utils/geminiService';
import MessageBubble from './components/MessageBubble';
import { Message, ConnectionStatus, Chat, Reminder, Attachment, Personality, ChatPayload, WeatherData } from './utils/types';
import { detectRoute } from './utils/modelRouter';
import { decodeBase64, decodeAudioData } from './utils/audioUtils';

const STORAGE_KEY_CHATS     = 'sakanfer_chats_v3';
const STORAGE_KEY_REMINDERS = 'sakanfer_reminders_v2';

function encode(b: Uint8Array) { let s = ''; for (let i = 0; i < b.byteLength; i++) s += String.fromCharCode(b[i]); return btoa(s); }
function pcmBlob(data: Float32Array, rate: number) {
  const i16 = new Int16Array(data.length);
  for (let i = 0; i < data.length; i++) { const v = Math.max(-1, Math.min(1, data[i])); i16[i] = v < 0 ? v * 0x8000 : v * 0x7FFF; }
  return { data: encode(new Uint8Array(i16.buffer)), mimeType: `audio/pcm;rate=${rate}` };
}

const parseDate = (day: string, time: string): Date => {
  const date = new Date(); const ct = time.toLowerCase().replace(/\s+/g, '');
  let h = 9, m = 0;
  const tm = ct.match(/(\d{1,2}):(\d{2})(am|pm)?/);
  if (tm) { h = parseInt(tm[1]); m = parseInt(tm[2]); if (tm[3] === 'pm' && h < 12) h += 12; if (tm[3] === 'am' && h === 12) h = 0; }
  else { const n = parseInt(ct); if (!isNaN(n)) h = n; }
  const d = day.toLowerCase();
  if (d.includes('ba3d ghodwa') || d.includes('after tomorrow')) date.setDate(date.getDate() + 2);
  else if (d.includes('ghodwa') || d.includes('tomorrow'))       date.setDate(date.getDate() + 1);
  else if (day.includes('-')) { const p = day.split('-'); if (p.length === 3) date.setFullYear(+p[0], +p[1] - 1, +p[2]); }
  date.setHours(h, m, 0, 0);
  if (date < new Date() && !day.includes('-') && !d.includes('ghodwa')) date.setDate(date.getDate() + 1);
  return date;
};

// ─── TTS Helper ───────────────────────────────────────────────────────────────
function speak(text: string) {
  if (!('speechSynthesis' in window)) return;
  window.speechSynthesis.cancel();
  const utt  = new SpeechSynthesisUtterance(text);
  utt.lang   = 'fr-FR'; // closest to Tunisian Arabic
  utt.rate   = 0.9;
  utt.volume = 1;
  window.speechSynthesis.speak(utt);
}

// ─── Export Chat ──────────────────────────────────────────────────────────────
function exportChat(chat: Chat) {
  const lines = [`# ${chat.title}\n`, `Date: ${chat.createdAt.toLocaleDateString()}\n\n`];
  chat.messages.forEach(m => {
    lines.push(`**${m.role === 'user' ? 'Inti' : 'Sakanfer'}** [${m.timestamp.toLocaleTimeString()}]`);
    lines.push(m.text); lines.push('');
  });
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown' });
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = `sakanfer-${chat.title.replace(/\s+/g, '-')}.md`; a.click();
}

// ─── App ──────────────────────────────────────────────────────────────────────
const App: React.FC = () => {
  const [chats, setChats] = useState<Chat[]>(() => {
    const s = localStorage.getItem(STORAGE_KEY_CHATS);
    if (s) try { return JSON.parse(s).map((c: any) => ({ ...c, messages: c.messages.map((m: any) => ({ ...m, timestamp: new Date(m.timestamp) })), createdAt: new Date(c.createdAt) })); } catch {}
    const id = Date.now().toString();
    return [{ id, title: 'New Conversation', createdAt: new Date(), messages: [{ id: 'w1', role: 'model' as const, text: 'Ahla! Sakanfer m3ak. Fash najjem n3awnek?', timestamp: new Date() }] }];
  });

  const [activeChatId, setActiveChatId]   = useState(chats[0].id);
  const [reminders, setReminders]         = useState<Reminder[]>(() => {
    const s = localStorage.getItem(STORAGE_KEY_REMINDERS);
    return s ? JSON.parse(s).map((r: any) => ({ ...r, dueTime: new Date(r.dueTime) })) : [];
  });

  const [toast, setToast]               = useState<{ message: string; type: 'error' | 'success' } | null>(null);
  const [permission, setPermission]     = useState<NotificationPermission>(Notification.permission);
  const [isQuota, setIsQuota]           = useState(false);
  const [location, setLocation]         = useState<{ lat: number; lng: number } | null>(null);
  const [input, setInput]               = useState('');
  const [attachment, setAttachment]     = useState<Attachment | null>(null);
  const [status, setStatus]             = useState<ConnectionStatus>(ConnectionStatus.IDLE);
  const [sidebarOpen, setSidebarOpen]   = useState(false);
  const [drawMode, setDrawMode]         = useState(false);
  const [isLive, setIsLive]             = useState(false);
  const [aiSpeaking, setAiSpeaking]     = useState(false);
  const [micActive, setMicActive]       = useState(true);
  const [volume, setVolume]             = useState(0);
  const [search, setSearch]             = useState('');

  const activeChat = chats.find(c => c.id === activeChatId) || chats[0];
  const messages   = activeChat.messages;

  const endRef       = useRef<HTMLDivElement>(null);
  const fileRef      = useRef<HTMLInputElement>(null);
  const audioCtxRef  = useRef<AudioContext | null>(null);
  const liveRef      = useRef<any>(null);
  const inputCtxRef  = useRef<AudioContext | null>(null);
  const streamRef    = useRef<MediaStream | null>(null);
  const nextTimeRef  = useRef(0);
  const sourcesSetRef = useRef<Set<AudioBufferSourceNode>>(new Set());
  const analyserRef  = useRef<AnalyserNode | null>(null);
  const recorderRef  = useRef<MediaRecorder | null>(null);
  const chunksRef    = useRef<Blob[]>([]);

  useEffect(() => { localStorage.setItem(STORAGE_KEY_CHATS,     JSON.stringify(chats)); },     [chats]);
  useEffect(() => { localStorage.setItem(STORAGE_KEY_REMINDERS, JSON.stringify(reminders)); }, [reminders]);

  useEffect(() => {
    if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission().then(p => setPermission(p));
    if ('geolocation' in navigator) navigator.geolocation.getCurrentPosition(pos => setLocation({ lat: pos.coords.latitude, lng: pos.coords.longitude }), () => {});
  }, []);

  useEffect(() => { if (toast) { const t = setTimeout(() => setToast(null), 3500); return () => clearTimeout(t); } }, [toast]);

  // ── Enhanced Alarm System ──────────────────────────────────────────────────
  useEffect(() => {
    const interval = setInterval(() => {
      const now = new Date();
      setReminders(prev => prev.map(r => {
        if (!r.isNotified && r.dueTime <= now && now.getTime() - r.dueTime.getTime() < 60000) {
          // 1. Vibrate (mobile)
          if ('vibrate' in navigator) navigator.vibrate([400, 200, 400, 200, 400]);

          // 2. TTS - speak the reminder
          speak(`Sakanfer! ${r.text}`);

          // 3. System notification
          if (Notification.permission === 'granted') {
            new Notification('⏰ Sakanfer', { body: `Tfakkar: ${r.text}`, icon: '/icon.png', tag: r.id });
          }

          // 4. Toast in UI
          setToast({ message: `⏰ ${r.text}`, type: 'success' });

          // Handle repeat
          if (r.repeat === 'daily') {
            const next = new Date(r.dueTime);
            next.setDate(next.getDate() + 1);
            return { ...r, dueTime: next, isNotified: false };
          } else if (r.repeat === 'weekly') {
            const next = new Date(r.dueTime);
            next.setDate(next.getDate() + 7);
            return { ...r, dueTime: next, isNotified: false };
          }

          return { ...r, isNotified: true };
        }
        return r;
      }));
    }, 3000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    let id: number;
    const tick = () => {
      if (analyserRef.current && isLive && micActive) {
        const d = new Uint8Array(analyserRef.current.frequencyBinCount);
        analyserRef.current.getByteFrequencyData(d);
        setVolume(d.reduce((a, b) => a + b) / d.length);
      } else setVolume(0);
      id = requestAnimationFrame(tick);
    };
    if (isLive) tick();
    return () => cancelAnimationFrame(id);
  }, [isLive, micActive]);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages, status]);

  // ── Helpers ───────────────────────────────────────────────────────────────
  const stopAudio = useCallback(() => {
    sourcesSetRef.current.forEach(s => { try { s.stop(); } catch {} });
    sourcesSetRef.current.clear(); setAiSpeaking(false);
  }, []);

  const playChunk = (buf: AudioBuffer) => {
    if (!audioCtxRef.current) return;
    const t = audioCtxRef.current.currentTime;
    if (nextTimeRef.current < t) nextTimeRef.current = t;
    const src = audioCtxRef.current.createBufferSource();
    src.buffer = buf; src.connect(audioCtxRef.current.destination);
    src.start(nextTimeRef.current); nextTimeRef.current += buf.duration;
    sourcesSetRef.current.add(src);
  };

  const addMsg = (msg: Message) =>
    setChats(p => p.map(c => c.id === activeChatId ? { ...c, messages: [...c.messages, msg] } : c));

  const updateMsg = (id: string, patch: Partial<Message>) =>
    setChats(p => p.map(c => c.id === activeChatId ? { ...c, messages: c.messages.map(m => m.id === id ? { ...m, ...patch } : m) } : c));

  // ── Auto-title after first user message ───────────────────────────────────
  const autoTitle = async (chatId: string, firstMsg: string) => {
    const title = await generateTitle(firstMsg);
    setChats(p => p.map(c => c.id === chatId ? { ...c, title } : c));
  };

  // ── Core Send ─────────────────────────────────────────────────────────────
  const runChat = async (history: ChatPayload['messages'], streamId: string) => {
    const payload: ChatPayload = {
      messages: history, personality: (activeChat.personality as Personality) || 'default',
      location, hasAttachment: history.some(m => m.parts.some(p => p.inlineData)),
    };

    await streamChat(
      payload,
      // onChunk
      (text, modelLabel, routeType) => setChats(p => p.map(c => c.id === activeChatId ? {
        ...c, messages: c.messages.map(m => m.id === streamId ? { ...m, text: m.text + text, modelLabel, routeType, isStreaming: true } : m)
      } : c)),
      // onFunctionCall
      (name, args, modelLabel) => {
        if (name === 'set_reminder') {
          const dueTime = parseDate(args.day || 'today', args.time || '09:00');
          setReminders(p => [...p, { id: Date.now().toString(), text: args.task, dueTime, isNotified: false, repeat: args.repeat || 'none' }]);
          setToast({ message: '⏰ Rappel enregistré!', type: 'success' });
          updateMsg(streamId, { text: `Mrigel! Mantansitkech f ${args.time} ✅`, modelLabel, isStreaming: false });
        } else if (name === 'generate_image') {
          updateMsg(streamId, { text: '🎨 D9i9a...', modelLabel, isStreaming: false });
          genImageById(args.prompt, streamId);
          return;
        }
        setStatus(ConnectionStatus.IDLE);
      },
      // onWeather
      (data: WeatherData) => {
        const t9es = data.condition === 'sunny' ? `Mchammes, ${data.temp}° 🌞` : data.condition === 'rainy' ? `Chta, ${data.temp}° 🌧️` : `${data.temp}°`;
        updateMsg(streamId, { text: `T9es ${data.city} lyoum: ${t9es}`, weatherData: data, modelLabel: 'Open-Meteo', routeType: 'weather', isStreaming: false });
        setStatus(ConnectionStatus.IDLE);
      },
      // onDone
      (sources) => {
        setChats(p => p.map(c => c.id === activeChatId ? {
          ...c, messages: c.messages.map(m => m.id === streamId ? { ...m, isStreaming: false, groundingSources: sources } : m)
        } : c));
        setStatus(ConnectionStatus.IDLE); setIsQuota(false);
      },
      // onError
      (msg, quota) => {
        updateMsg(streamId, { text: msg, hasError: true, isStreaming: false });
        setStatus(ConnectionStatus.IDLE);
        if (quota) setIsQuota(true); else setToast({ message: msg, type: 'error' });
      }
    );
  };

  const handleSend = async () => {
    const text = input.trim();
    if (!text && !attachment && !drawMode) return;
    if (Notification.permission === 'default') Notification.requestPermission();

    if (drawMode) {
      addMsg({ id: Date.now().toString(), role: 'user', text, timestamp: new Date() });
      setInput(''); setDrawMode(false);
      genImage(text); return;
    }

    if (!audioCtxRef.current) audioCtxRef.current = new (window.AudioContext || (window as any).webkitAudioContext)({ sampleRate: 24000 });
    if (audioCtxRef.current.state === 'suspended') try { await audioCtxRef.current.resume(); } catch {}

    const att = attachment;
    setInput(''); setAttachment(null); stopAudio();
    setStatus(ConnectionStatus.PROCESSING); setIsQuota(false);

    const userMsg: Message = { id: Date.now().toString(), role: 'user', text, timestamp: new Date(), attachment: att || undefined };
    addMsg(userMsg);

    // Auto-title if first real user message
    const userCount = messages.filter(m => m.role === 'user').length;
    if (userCount === 0) autoTitle(activeChatId, text);

    const streamId = (Date.now() + 1).toString();
    addMsg({ id: streamId, role: 'model', text: '', timestamp: new Date(), isStreaming: true });

    const hist = [...messages, userMsg].slice(-15).map(m => {
      const parts: any[] = [];
      if (m.attachment) parts.push({ inlineData: { mimeType: m.attachment.mimeType, data: m.attachment.data } });
      if (m.text) parts.push({ text: m.text });
      return { role: m.role, parts };
    });
    await runChat(hist, streamId);
  };

  const handleEdit = async (messageId: string, newText: string) => {
    const chat = chats.find(c => c.id === activeChatId); if (!chat) return;
    const idx  = chat.messages.findIndex(m => m.id === messageId); if (idx === -1) return;
    const trim = [...chat.messages.slice(0, idx + 1)];
    trim[idx]  = { ...trim[idx], text: newText, timestamp: new Date() };
    setChats(p => p.map(c => c.id === activeChatId ? { ...c, messages: trim } : c));
    setStatus(ConnectionStatus.PROCESSING); stopAudio(); setIsQuota(false);

    const streamId = Date.now().toString();
    setChats(p => p.map(c => c.id === activeChatId ? { ...c, messages: [...trim, { id: streamId, role: 'model' as const, text: '', timestamp: new Date(), isStreaming: true }] } : c));

    const hist = trim.slice(-15).map(m => {
      const parts: any[] = [];
      if (m.attachment) parts.push({ inlineData: { mimeType: m.attachment.mimeType, data: m.attachment.data } });
      if (m.text) parts.push({ text: m.text });
      return { role: m.role, parts };
    });
    await runChat(hist, streamId);
  };

  const genImage = async (prompt: string, att?: Attachment) => {
    setStatus(ConnectionStatus.PROCESSING);
    const id = Date.now().toString();
    addMsg({ id, role: 'model', text: '🎨 Qa3ed yrasm...', timestamp: new Date(), isStreaming: true });
    const { imageUrls, model, error } = await generateImageViaApi(prompt, att);
    if (imageUrls?.length) updateMsg(id, { text: "Tfaddel! 🎨", imageUrls, modelLabel: model || 'Imagen 3', routeType: 'vision', isStreaming: false });
    else { updateMsg(id, { text: error || 'Famma moshkla.', hasError: true, isStreaming: false }); if (error?.toLowerCase().includes('quota')) setIsQuota(true); }
    setStatus(ConnectionStatus.IDLE); setDrawMode(false);
  };

  const genImageById = async (prompt: string, msgId: string) => {
    const { imageUrls, model } = await generateImageViaApi(prompt);
    if (imageUrls?.length) updateMsg(msgId, { text: "Tfaddel! 🎨", imageUrls, modelLabel: model, isStreaming: false });
    else updateMsg(msgId, { text: 'Famma moshkla fil image.', hasError: true, isStreaming: false });
    setStatus(ConnectionStatus.IDLE);
  };

  const toggleRec = useCallback(async () => {
    if (status === ConnectionStatus.RECORDING) { if (recorderRef.current?.state !== 'inactive') recorderRef.current?.stop(); return; }
    try {
      stopAudio();
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec    = new MediaRecorder(stream);
      recorderRef.current = rec; chunksRef.current = [];
      rec.ondataavailable = e => { if (e.data.size > 0) chunksRef.current.push(e.data); };
      rec.onstop = async () => {
        setStatus(ConnectionStatus.PROCESSING);
        const mime = rec.mimeType || 'audio/webm';
        const blob = new Blob(chunksRef.current, { type: mime });
        const reader = new FileReader(); reader.readAsDataURL(blob);
        reader.onloadend = async () => {
          const b64  = (reader.result as string).split(',')[1];
          const text = await transcribeViaApi(b64, mime);
          if (text) setInput(p => (p ? p + ' ' : '') + text);
          else setToast({ message: 'Erreur transcription', type: 'error' });
          setStatus(ConnectionStatus.IDLE); stream.getTracks().forEach(t => t.stop());
        };
      };
      rec.start(); setStatus(ConnectionStatus.RECORDING);
    } catch { setToast({ message: 'Mic ma ysupportish', type: 'error' }); }
  }, [status, stopAudio]);

  const endLive = async () => {
    if (liveRef.current)   { try { await liveRef.current.close(); } catch {} liveRef.current = null; }
    if (streamRef.current) { streamRef.current.getTracks().forEach(t => t.stop()); streamRef.current = null; }
    if (inputCtxRef.current)  { try { await inputCtxRef.current.close();  } catch {} inputCtxRef.current  = null; }
    if (audioCtxRef.current)  { try { await audioCtxRef.current.close();  } catch {} audioCtxRef.current  = null; }
    stopAudio(); setIsLive(false); setAiSpeaking(false); setMicActive(false);
    setStatus(ConnectionStatus.IDLE); nextTimeRef.current = 0;
  };

  const startLive = async () => {
    await endLive(); stopAudio();
    setIsLive(true); setStatus(ConnectionStatus.LIVE_CALL); setMicActive(true);

    try {
      const apiKey = await getLiveApiKey();
      if (!apiKey) throw new Error('No key');

      const ai       = new GoogleGenAI({ apiKey });
      const inCtx    = new (window.AudioContext || (window as any).webkitAudioContext)({ latencyHint: 'interactive', sampleRate: 16000 });
      const outCtx   = new (window.AudioContext || (window as any).webkitAudioContext)({ sampleRate: 24000, latencyHint: 'interactive' });
      inputCtxRef.current = inCtx; audioCtxRef.current = outCtx;
      if (inCtx.state  === 'suspended') await inCtx.resume();
      if (outCtx.state === 'suspended') await outCtx.resume();

      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1, sampleRate: 16000 } as any });
      if (inputCtxRef.current !== inCtx) { stream.getTracks().forEach(t => t.stop()); return; }
      streamRef.current = stream;

      const src      = inCtx.createMediaStreamSource(stream);
      const analyser = inCtx.createAnalyser(); analyser.fftSize = 256;
      src.connect(analyser); analyserRef.current = analyser;

      const ctx = messages.slice(-5).map(m => `${m.role === 'user' ? 'User' : 'Sakanfer'}: ${m.text}`).join('\n');
      const sys = getChatoPrompt(ctx, activeChat.personality || 'default');

      const proc = inCtx.createScriptProcessor(4096, 1, 1);
      src.connect(proc); proc.connect(inCtx.destination);
      proc.onaudioprocess = (e) => {
        if (!liveRef.current || !micActive) return;
        const blob = pcmBlob(e.inputBuffer.getChannelData(0), inCtx.sampleRate);
        try { liveRef.current.sendRealtimeInput({ media: blob }); } catch {}
      };

      liveRef.current = await ai.live.connect({
        model: 'gemini-2.5-flash-native-audio-preview-12-2025',
        callbacks: {
          onopen:    () => setMicActive(true),
          onmessage: async (m: any) => {
            if (m.serverContent?.interrupted) { stopAudio(); setMicActive(true); }
            const b64 = m.serverContent?.modelTurn?.parts?.[0]?.inlineData?.data;
            if (b64 && audioCtxRef.current?.state !== 'closed') {
              setAiSpeaking(true);
              const buf = await decodeAudioData(decodeBase64(b64), audioCtxRef.current!, 24000, 1);
              playChunk(buf);
            }
            if (m.serverContent?.turnComplete) { setAiSpeaking(false); setMicActive(true); }
          },
          onclose: () => endLive(),
          onerror: () => { endLive(); setToast({ message: 'Live Error', type: 'error' }); },
        },
        config: {
          responseModalities: [Modality.AUDIO],
          speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: 'Fenrir' } } },
          systemInstruction: sys,
        }
      });
    } catch { endLive(); setToast({ message: 'Erreur Live (Quota/Net)', type: 'error' }); }
  };

  const newChat = () => {
    const id = Date.now().toString();
    setChats(p => [{ id, title: 'New Conversation', createdAt: new Date(), messages: [{ id: 'w1', role: 'model' as const, text: 'Ahla! Sakanfer m3ak. Fash najjem n3awnek?', timestamp: new Date() }] }, ...p]);
    setActiveChatId(id); setSidebarOpen(false); stopAudio(); setInput(''); setAttachment(null); setIsQuota(false);
  };

  const handleFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]; if (!f) return;
    const r = new FileReader(); r.onloadend = () => setAttachment({ mimeType: f.type, data: (r.result as string).split(',')[1], name: f.name });
    r.readAsDataURL(f);
  };

  const filteredChats = search
    ? chats.filter(c => c.title.toLowerCase().includes(search.toLowerCase()) || c.messages.some(m => m.text.toLowerCase().includes(search.toLowerCase())))
    : chats;

  const route = detectRoute(input, !!attachment);

  return (
    <div className="flex h-[100dvh] bg-[#050505] text-zinc-100 overflow-hidden relative">

      {/* Toast */}
      {toast && (
        <div className={`fixed top-4 left-1/2 -translate-x-1/2 px-6 py-3 rounded-full shadow-2xl z-[200] flex items-center gap-3 animate-fade-in-down backdrop-blur-md ${toast.type === 'error' ? 'bg-red-500/90' : 'bg-green-500/90'}`}>
          {toast.type === 'error' ? <AlertCircle size={18} className="text-white" /> : <Bell size={18} className="text-white" />}
          <span className="text-sm font-bold text-white">{toast.message}</span>
        </div>
      )}

      {/* Notification prompt */}
      {permission === 'default' && (
        <div onClick={() => Notification.requestPermission().then(p => setPermission(p))}
          className="fixed top-20 left-1/2 -translate-x-1/2 bg-blue-600 px-4 py-2 rounded-lg z-[200] shadow-xl flex items-center gap-3 cursor-pointer">
          <Bell size={16} className="animate-pulse" />
          <span className="text-xs font-bold">Fa33el el notifications</span>
        </div>
      )}

      {/* Sidebar overlay */}
      {sidebarOpen && <div onClick={() => setSidebarOpen(false)} className="fixed inset-0 bg-black/80 backdrop-blur-md z-[105]" />}

      {/* ── Sidebar ────────────────────────────────────────────────────────── */}
      <div className={`fixed inset-y-0 right-0 w-80 bg-[#0a0a0c] border-l border-white/5 transform transition-transform duration-500 z-[110] flex flex-col ${sidebarOpen ? 'translate-x-0' : 'translate-x-full'}`}>
        <div className="p-5 safe-top flex flex-col h-full">
          {/* Header */}
          <div className="flex items-center justify-between mb-5">
            <h2 className="text-[10px] font-black uppercase tracking-[0.3em] text-zinc-500">Sakanfer AI</h2>
            <button onClick={() => setSidebarOpen(false)} className="p-2 hover:bg-white/5 rounded-full"><X size={20} /></button>
          </div>

          <button onClick={newChat} className="flex items-center gap-3 w-full p-4 rounded-2xl bg-white/5 border border-white/10 hover:bg-white/10 transition-all text-xs font-bold mb-4">
            <Plus size={16} /> M7adtha Jdida
          </button>

          {/* Search */}
          <div className="relative mb-3">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-zinc-600" />
            <input value={search} onChange={e => setSearch(e.target.value)} placeholder="D9aw f l'mha9thath..."
              className="w-full bg-zinc-900 border border-white/10 rounded-xl pl-9 pr-4 py-2.5 text-xs text-zinc-300 outline-none placeholder:text-zinc-600 focus:border-violet-500/50 transition-colors" />
          </div>

          {/* Chat List */}
          <div className="flex-1 overflow-y-auto space-y-1.5 no-scrollbar">
            {filteredChats.map(chat => (
              <div key={chat.id} onClick={() => { setActiveChatId(chat.id); setSidebarOpen(false); }}
                className={`group flex items-center justify-between p-3.5 rounded-xl cursor-pointer transition-all ${activeChatId === chat.id ? 'bg-violet-600/10 border border-violet-500/30 text-white' : 'hover:bg-white/5 text-zinc-500'}`}>
                <div className="flex flex-col flex-1 min-w-0">
                  <span className="text-xs font-bold truncate">{chat.title}</span>
                  <span className="text-[10px] text-zinc-600">{chat.messages.length - 1} msg</span>
                </div>
                <div className="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                  <button onClick={e => { e.stopPropagation(); exportChat(chat); }} className="p-1.5 hover:text-blue-400 transition-colors" title="Export"><Download size={12} /></button>
                  <button onClick={e => { e.stopPropagation(); setChats(p => { const f = p.filter(c => c.id !== chat.id); if (activeChatId === chat.id && f.length > 0) setActiveChatId(f[0].id); return f.length ? f : [{ id: Date.now().toString(), title: 'New Conversation', createdAt: new Date(), messages: [{ id: 'w1', role: 'model', text: 'Ahla!', timestamp: new Date() }] }]; }); }}
                    className="p-1.5 hover:text-red-400 transition-colors"><Trash2 size={12} /></button>
                </div>
              </div>
            ))}
            {filteredChats.length === 0 && <div className="text-center text-zinc-700 text-xs py-8">Mafamma ntiha9</div>}
          </div>

          {/* Reminders */}
          <div className="mt-4 pt-4 border-t border-white/5">
            <div className="flex items-center gap-2 mb-3">
              <Bell size={13} className="text-zinc-500" />
              <span className="text-[10px] font-bold uppercase tracking-wider text-zinc-500">Tafkirat ({reminders.filter(r => !r.isNotified).length})</span>
            </div>
            <div className="space-y-1.5 max-h-36 overflow-y-auto no-scrollbar">
              {reminders.filter(r => !r.isNotified).map(r => (
                <div key={r.id} className="p-3 rounded-xl bg-white/5 border border-white/5 flex items-center justify-between">
                  <div className="flex flex-col flex-1 min-w-0">
                    <span className="text-xs font-medium text-zinc-300 truncate">{r.text}</span>
                    <span className="text-[9px] text-zinc-500">
                      {r.dueTime.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                      {r.repeat !== 'none' && ` · ${r.repeat}`}
                    </span>
                  </div>
                  <button onClick={() => setReminders(p => p.filter(x => x.id !== r.id))} className="text-zinc-600 hover:text-red-400 transition-colors ml-2"><X size={13} /></button>
                </div>
              ))}
              {reminders.filter(r => !r.isNotified).length === 0 && <div className="text-[10px] text-zinc-700 text-center py-2">Mafamma chay</div>}
            </div>
          </div>
        </div>
      </div>

      {/* ── Main ─────────────────────────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col relative h-full">

        {/* Header */}
        <header className="safe-top h-14 px-4 flex items-center justify-between bg-[#050505]/95 backdrop-blur-xl z-[100] sticky top-0 border-b border-white/[0.03]">
          <div className="flex items-center gap-3">
            <button onClick={() => setSidebarOpen(true)} className="p-2 hover:bg-white/5 rounded-full"><Menu size={20} /></button>
            <h1 className="text-sm font-black uppercase tracking-tighter italic">𝖘𝖆𝖐𝖆𝖓𝖋𝖊𝖗</h1>
            <select value={activeChat.personality || 'default'}
              onChange={e => setChats(p => p.map(c => c.id === activeChatId ? { ...c, personality: e.target.value as Personality } : c))}
              className="bg-zinc-900 text-xs text-zinc-300 border border-white/10 rounded-lg px-2 py-1.5 outline-none cursor-pointer hover:bg-zinc-800">
              <option value="default">Normal</option>
              <option value="history">🏛️ Tarikh</option>
              <option value="friend">🤝 Sa7bi</option>
              <option value="comedy">😂 Dahkni</option>
            </select>
          </div>
          <div className="flex items-center gap-3">
            {input && (
              <span className={`hidden sm:flex items-center gap-1 text-[9px] font-bold uppercase tracking-wider ${route.color} bg-white/5 px-2 py-0.5 rounded-full border border-white/5`}>
                {route.emoji} {route.label}
              </span>
            )}
            <div className="flex items-center gap-1.5">
              <span className={`w-1.5 h-1.5 rounded-full animate-pulse ${isQuota ? 'bg-red-500' : 'bg-green-500'}`} />
              <span className="text-[10px] text-white/40">{isQuota ? 'QUOTA' : 'ONLINE'}</span>
            </div>
          </div>
        </header>

        {/* Messages */}
        <main className="flex-1 overflow-y-auto px-4 py-6 no-scrollbar flex flex-col">
          {isQuota && (
            <div className="mb-6 p-4 bg-red-950/30 border border-red-500/30 rounded-2xl flex items-center gap-4 mx-auto max-w-3xl w-full">
              <div className="p-2 bg-red-500/20 rounded-full text-red-400 shrink-0"><AlertCircle size={22} /></div>
              <div><div className="text-sm font-bold text-red-200">Quota Wfet</div><div className="text-xs text-red-300/80">Kol el API keys wfet. Zid mfate7 jadda wella 3awed mba3d.</div></div>
            </div>
          )}

          {isLive ? (
            <div className="flex-1 flex flex-col items-center justify-center space-y-16">
              <div className="relative w-64 h-64 flex items-center justify-center cursor-pointer" onClick={() => { if (aiSpeaking) { stopAudio(); setMicActive(true); } else setMicActive(p => !p); }}>
                {micActive && !aiSpeaking && volume > 5 && (
                  <>
                    <div className="absolute w-full h-full rounded-full border border-blue-500/30 animate-[ping_1.5s_ease-out_infinite]" />
                    <div className="absolute w-[80%] h-[80%] rounded-full border border-blue-500/50 animate-[ping_1.5s_ease-out_infinite_0.2s]" />
                  </>
                )}
                <div className={`relative z-10 w-36 h-36 rounded-full bg-[#0a0a0c] border flex items-center justify-center transition-all duration-300 ${aiSpeaking ? 'border-violet-500 shadow-[0_0_60px_rgba(124,58,237,0.5)] scale-110' : micActive ? 'border-blue-500/50 shadow-[0_0_40px_rgba(59,130,246,0.3)]' : 'border-white/10'}`}>
                  {aiSpeaking
                    ? <div className="flex items-center gap-1 h-10">{[16,28,40,28,16].map((h, i) => <div key={i} className="w-1.5 bg-violet-400 rounded-full animate-[bounce_0.5s_infinite]" style={{ height: h, animationDelay: `${i * 0.1}s` }} />)}</div>
                    : micActive ? <Mic size={30} className="text-blue-500" style={{ transform: `scale(${1 + volume / 100})` }} />
                    : <div className="w-8 h-8 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />}
                </div>
                <div className="absolute -bottom-12 text-center w-full">
                  <span className="text-[11px] font-bold uppercase tracking-widest text-white/40 bg-black/50 px-3 py-1 rounded-full border border-white/5">
                    {aiSpeaking ? 'Yehki — Anzel t9oss' : micActive ? 'Anzel t7ki' : 'Ykhammem...'}
                  </span>
                </div>
              </div>
              <button onClick={endLive} className="px-10 py-4 bg-red-600 hover:bg-red-700 text-white rounded-full font-black text-sm uppercase tracking-widest flex items-center gap-3 shadow-[0_0_30px_rgba(220,38,38,0.4)]">
                <PhoneOff size={22} />
              </button>
            </div>
          ) : (
            <div className="max-w-3xl mx-auto w-full space-y-6 pb-52">
              {messages.map(m => <MessageBubble key={m.id} message={m} onEdit={handleEdit} />)}
              {status === ConnectionStatus.PROCESSING && !messages.at(-1)?.isStreaming && (
                <div className="flex justify-center py-4"><Loader2 className="animate-spin text-violet-500 opacity-40" size={20} /></div>
              )}
              <div ref={endRef} />
            </div>
          )}
        </main>

        {/* ── Input Area ──────────────────────────────────────────────────── */}
        {!isLive && (
          <div className="fixed bottom-0 left-0 w-full p-4 z-[100] safe-bottom bg-gradient-to-t from-[#050505] via-[#050505]/95 to-transparent">
            <div className="max-w-3xl mx-auto flex flex-col gap-3">

              {attachment && (
                <div className="mr-auto bg-[#1c1c1e] px-4 py-2 rounded-xl flex items-center gap-3 border border-white/10 shadow-xl">
                  <div className="p-1.5 bg-blue-600/20 rounded-lg text-blue-400">
                    {attachment.mimeType.startsWith('image/') ? <ImageIcon size={14} /> : <FileText size={14} />}
                  </div>
                  <span className="text-xs text-zinc-300 max-w-[150px] truncate">{attachment.name}</span>
                  <button onClick={() => setAttachment(null)} className="p-1 hover:text-red-400"><X size={16} /></button>
                </div>
              )}

              <div className="w-full bg-[#121214] border border-white/10 rounded-[2rem] shadow-2xl overflow-hidden">
                <div className="flex items-center gap-1 p-2 border-b border-white/5 bg-white/[0.02]">
                  <input type="file" ref={fileRef} onChange={handleFile} className="hidden" accept="image/*,application/pdf" />
                  <button onClick={() => fileRef.current?.click()} className={`p-3 rounded-full hover:bg-white/10 transition-colors ${attachment ? 'text-blue-400' : 'text-zinc-400'}`}>
                    <Paperclip size={18} />
                  </button>
                  <button onClick={() => setDrawMode(!drawMode)} className={`p-3 rounded-full hover:bg-white/10 transition-colors ${drawMode ? 'text-blue-400' : 'text-zinc-400'}`}>
                    <Palette size={18} />
                  </button>
                  <div className="h-4 w-[1px] bg-white/10 mx-1" />
                  <button onClick={startLive} className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-violet-500/10 hover:bg-violet-500/20 border border-violet-500/30 text-violet-300 transition-all ml-auto">
                    <div className="relative"><span className="absolute inset-0 rounded-full bg-violet-400 animate-ping opacity-20" /><AudioLines size={16} /></div>
                    <span className="text-[10px] font-bold uppercase tracking-wider">A7ki m3aya</span>
                  </button>
                </div>

                <div className="flex items-end gap-2 p-2">
                  <textarea value={input} onChange={e => setInput(e.target.value)}
                    onKeyDown={e => e.key === 'Enter' && !e.shiftKey && (e.preventDefault(), handleSend())}
                    placeholder={
                      status === ConnectionStatus.RECORDING  ? 'Tkallem...' :
                      status === ConnectionStatus.PROCESSING ? 'Qa3ed yekteb...' :
                      drawMode ? 'Sawerli taswira...' :
                      attachment ? 'Chnouwa n3amlou bih?' : 'Aktob 7aja...'
                    }
                    className="flex-1 bg-transparent border-none focus:ring-0 text-white py-3 px-4 text-base resize-none max-h-32 no-scrollbar placeholder:text-zinc-600"
                    rows={1} dir="rtl" />
                  <div className="flex items-center gap-1 pb-1 pr-1">
                    <button onClick={toggleRec} className={`p-3 rounded-full transition-all ${status === ConnectionStatus.RECORDING ? 'bg-red-600 text-white animate-pulse' : 'bg-white/5 text-zinc-400 hover:text-white'}`}>
                      {status === ConnectionStatus.RECORDING ? <Square size={20} fill="currentColor" /> : <Mic size={20} />}
                    </button>
                    <button onClick={handleSend} disabled={!input.trim() && !attachment && !drawMode}
                      className={`p-3 rounded-full transition-all ${input.trim() || attachment || drawMode ? 'bg-violet-600 text-white shadow-[0_0_20px_rgba(124,58,237,0.4)]' : 'bg-zinc-800 text-zinc-600'}`}>
                      <Send size={20} />
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default App;
