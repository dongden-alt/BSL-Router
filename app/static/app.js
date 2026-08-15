let globalConfig = {};
let currentView = 'list';
let activeProviderId = null;
let isEditingVisibility = false;
let epBansState = {};  // key: "provider/model" → ban entry (top-level to avoid TDZ)

// Resolved at runtime from KNOWN_PROVIDERS — no stale map needed
const _providerNameCache = {};

const SVGS = {
    openai: `<svg viewBox="0 0 24 24" fill="#10a37f" height="22" width="22"><path d="M22.28 11.23c.31-1.39-.06-2.82-.99-3.87-1.1-1.22-2.78-1.57-4.23-1.01-1.07-2.31-3.64-3.41-6.13-2.6-1.56.5-2.83 1.76-3.33 3.33-.56-1.45-2.24-2.11-3.46-1.01-.93 1.05-1.3 2.48-.99 3.87-1.45.56-2.11 2.24-1.01 3.46 1.05.93 2.48 1.3 3.87.99-1.22 1.1-1.57 2.78-1.01 4.23.5 1.56 1.76 2.83 3.33 3.33 1.56-.5 2.83-1.76 3.33-3.33.56 1.45 2.24 2.11 3.46 1.01.93-1.05 1.3-2.48.99-3.87 1.45-.56 2.11-2.24 1.01-3.46z"/></svg>`,
    anthropic: `<svg viewBox="0 0 24 24" fill="#d97757" height="22" width="22"><path d="M17.15 3.35h-3.75l-9.15 17.3h3.75l1.9-3.7h7.2l1.9 3.7h3.75l-9.15-17.3zm-6.05 10.45l2.6-5.15 2.6 5.15h-5.2z"/></svg>`,
    gemini: `<svg viewBox="0 0 24 24" fill="#1a73e8" height="22" width="22"><path d="M12 2C12 7.52 16.48 12 22 12C16.48 12 12 16.48 12 22C12 16.48 7.52 12 2 12C7.52 12 12 7.52 12 2Z"/></svg>`,
    groq: `<svg viewBox="0 0 32 32" fill="#f55036" height="22" width="22"><path d="M16 4a12 12 0 100 24A12 12 0 0016 4zm0 4a8 8 0 110 16A8 8 0 0116 8zm0 3a5 5 0 100 10A5 5 0 0016 11z"/></svg>`,
    mistral: `<svg viewBox="0 0 24 24" fill="#f3a052" height="22" width="22"><rect x="2" y="2" width="5" height="5"/><rect x="9" y="2" width="5" height="5"/><rect x="16" y="2" width="6" height="5"/><rect x="2" y="9" width="5" height="5"/><rect x="16" y="9" width="6" height="5"/><rect x="2" y="16" width="5" height="6"/><rect x="9" y="16" width="5" height="6"/><rect x="16" y="16" width="6" height="6"/></svg>`,
    deepseek: `<svg viewBox="0 0 24 24" fill="#4d6bfe" height="22" width="22"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>`,
    openrouter: `<svg viewBox="0 0 24 24" fill="none" stroke="#333333" stroke-width="2" height="22" width="22"><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/></svg>`,
    xai: `<svg viewBox="0 0 24 24" fill="#000000" height="22" width="22"><path d="M17.75 3h-4.5L3 21h4.5l3-6h3l3 6H21L17.75 3zm-3 9l1.5-3 1.5 3h-3z"/></svg>`,
    kiro: `<svg viewBox="0 0 24 24" fill="#9b51e0" height="22" width="22"><rect x="3" y="3" width="7.5" height="7.5" rx="1"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="1"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="1"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1"/></svg>`,
    cursor: `<svg viewBox="0 0 24 24" fill="#000000" height="22" width="22"><path d="M4 4l5.3 16 3.1-6.9L19.3 10 4 4z"/></svg>`,
    github: `<svg viewBox="0 0 24 24" fill="#333333" height="22" width="22"><path d="M12 2C6.477 2 2 6.477 2 12c0 4.42 2.865 8.166 6.839 9.489.5.092.682-.217.682-.482 0-.237-.008-.866-.013-1.7-2.782.603-3.369-1.34-3.369-1.34-.454-1.156-1.11-1.463-1.11-1.463-.908-.62.069-.608.069-.608 1.003.07 1.531 1.03 1.531 1.03.892 1.529 2.341 1.087 2.91.831.092-.646.35-1.086.636-1.336-2.22-.253-4.555-1.11-4.555-4.943 0-1.091.39-1.984 1.029-2.683-.103-.253-.446-1.27.098-2.647 0 0 .84-.269 2.75 1.025A9.578 9.578 0 0112 6.836c.85.004 1.705.114 2.504.336 1.909-1.294 2.747-1.025 2.747-1.025.546 1.379.203 2.394.1 2.647.64.699 1.028 1.592 1.028 2.683 0 3.842-2.339 4.687-4.566 4.935.359.309.678.919.678 1.852 0 1.336-.012 2.415-.012 2.743 0 .267.18.577.688.48C19.138 20.161 22 16.418 22 12c0-5.523-4.477-10-10-10z"/></svg>`,
    duckduckgo: `<svg viewBox="0 0 24 24" fill="#de5833" height="22" width="22"><circle cx="12" cy="10" r="6"/><path d="M12 16c-3.5 0-6 1.5-6 3v1h12v-1c0-1.5-2.5-3-6-3z"/></svg>`,
    claude_code: `<svg viewBox="0 0 24 24" fill="#d97757" height="22" width="22"><path d="M17.15 3.35h-3.75l-9.15 17.3h3.75l1.9-3.7h7.2l1.9 3.7h3.75l-9.15-17.3zm-6.05 10.45l2.6-5.15 2.6 5.15h-5.2z"/></svg>`,
    antigravity: `<svg viewBox="0 0 24 24" fill="#000" height="22" width="22"><path d="M12 2L2 22h5l2.5-5h5l2.5 5h5L12 2zm-1.5 11l1.5-3 1.5 3h-3z"/></svg>`,
    openai_codex: `<svg viewBox="0 0 24 24" fill="#10a37f" height="22" width="22"><path d="M22.28 11.23c.31-1.39-.06-2.82-.99-3.87-1.1-1.22-2.78-1.57-4.23-1.01-1.07-2.31-3.64-3.41-6.13-2.6-1.56.5-2.83 1.76-3.33 3.33-.56-1.45-2.24-2.11-3.46-1.01-.93 1.05-1.3 2.48-.99 3.87-1.45.56-2.11 2.24-1.01 3.46 1.05.93 2.48 1.3 3.87.99-1.22 1.1-1.57 2.78-1.01 4.23.5 1.56 1.76 2.83 3.33 3.33 1.56-.5 2.83-1.76 3.33-3.33.56 1.45 2.24 2.11 3.46 1.01.93-1.05 1.3-2.48.99-3.87 1.45-.56 2.11-2.24 1.01-3.46z"/></svg>`,
    kilo: `<svg viewBox="0 0 24 24" fill="#ffd700" height="22" width="22"><rect x="2" y="2" width="20" height="20" rx="4"/><path d="M8 6v12h3v-4.5l3.5 4.5h4l-4.5-5.5L18 6h-4l-3 4V6H8z" fill="#000"/></svg>`,
    xai_grok: `<svg viewBox="0 0 24 24" fill="#000000" height="22" width="22"><path d="M17.75 3h-4.5L3 21h4.5l3-6h3l3 6H21L17.75 3zm-3 9l1.5-3 1.5 3h-3z"/></svg>`,
    mimo_free: `<svg viewBox="0 0 24 24" fill="#ff6900" height="22" width="22"><rect x="2" y="2" width="20" height="20" rx="4"/><text x="12" y="16" font-size="12" font-family="sans-serif" font-weight="bold" fill="#fff" text-anchor="middle">MI</text></svg>`,
    ollama_cloud: `<svg viewBox="0 0 24 24" fill="#000" height="22" width="22"><circle cx="12" cy="12" r="10"/><circle cx="9" cy="10" r="2" fill="#fff"/><circle cx="15" cy="10" r="2" fill="#fff"/><path d="M10 15h4" stroke="#fff" stroke-width="2"/></svg>`,
    ollama_local: `<svg viewBox="0 0 24 24" fill="#000" height="22" width="22"><circle cx="12" cy="12" r="10"/><circle cx="9" cy="10" r="2" fill="#fff"/><circle cx="15" cy="10" r="2" fill="#fff"/><path d="M10 15h4" stroke="#fff" stroke-width="2"/></svg>`,
    nvidia_nim: `<svg viewBox="0 0 24 24" fill="#76b900" height="22" width="22"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8zm0-14c-3.31 0-6 2.69-6 6s2.69 6 6 6 6-2.69 6-6-2.69-6-6-6zm0 10c-2.21 0-4-1.79-4-4s1.79-4 4-4 4 1.79 4 4-1.79 4-4 4z"/></svg>`,
    cloudflare: `<svg viewBox="0 0 24 24" fill="#f38020" height="22" width="22"><path d="M17 14h-10c-1.66 0-3-1.34-3-3 0-1.3.84-2.4 2-2.82V8c0-2.21 1.79-4 4-4 1.5 0 2.8.84 3.5 2.1.34-.06.69-.1 1.05-.1 2.21 0 4 1.79 4 4v.1c1.1.2 2 1.1 2 2.2 0 1.3-1.1 2.4-2.5 2.4h-1.05z"/></svg>`,
    byteplus: `<svg viewBox="0 0 24 24" fill="#1a73e8" height="22" width="22"><path d="M4 20h16v-8l-8-6-8 6v8zm4-4h8v-4l-4-3-4 3v4z"/></svg>`,
    command_code: `<svg viewBox="0 0 24 24" fill="none" stroke="#000" stroke-width="2" height="22" width="22"><path d="M18 3a3 3 0 00-3 3v12a3 3 0 003 3 3 3 0 003-3 3 3 0 00-3-3H6a3 3 0 00-3 3 3 3 0 003 3 3 3 0 003-3V6a3 3 0 00-3-3 3 3 0 00-3 3 3 3 0 003 3h12a3 3 0 003-3 3 3 0 00-3-3z"/></svg>`,
    alibaba: `<svg viewBox="0 0 24 24" fill="#ff6a00" height="22" width="22"><path d="M12 2a10 10 0 100 20 10 10 0 000-20zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8zm-3-9c-.55 0-1-.45-1-1s.45-1 1-1 1 .45 1 1-.45 1-1 1zm6 0c-.55 0-1-.45-1-1s.45-1 1-1 1 .45 1 1-.45 1-1 1zm-3 4c-1.66 0-3-1.34-3-3h6c0 1.66-1.34 3-3 3z"/></svg>`,
    alibaba_intl: `<svg viewBox="0 0 24 24" fill="#ff6a00" height="22" width="22"><path d="M12 2a10 10 0 100 20 10 10 0 000-20zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8zm-3-9c-.55 0-1-.45-1-1s.45-1 1-1 1 .45 1 1-.45 1-1 1zm6 0c-.55 0-1-.45-1-1s.45-1 1-1 1 .45 1 1-.45 1-1 1zm-3 4c-1.66 0-3-1.34-3-3h6c0 1.66-1.34 3-3 3z"/></svg>`,
    azure_openai: `<svg viewBox="0 0 24 24" fill="#0078d4" height="22" width="22"><path d="M22.28 11.23c.31-1.39-.06-2.82-.99-3.87-1.1-1.22-2.78-1.57-4.23-1.01-1.07-2.31-3.64-3.41-6.13-2.6-1.56.5-2.83 1.76-3.33 3.33-.56-1.45-2.24-2.11-3.46-1.01-.93 1.05-1.3 2.48-.99 3.87-1.45.56-2.11 2.24-1.01 3.46 1.05.93 2.48 1.3 3.87.99-1.22 1.1-1.57 2.78-1.01 4.23.5 1.56 1.76 2.83 3.33 3.33 1.56-.5 2.83-1.76 3.33-3.33.56 1.45 2.24 2.11 3.46 1.01.93-1.05 1.3-2.48.99-3.87 1.45-.56 2.11-2.24 1.01-3.46z"/></svg>`,
    blackbox: `<svg viewBox="0 0 24 24" fill="#000" height="22" width="22"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>`,
    cerebras: `<svg viewBox="0 0 24 24" fill="#e52b2b" height="22" width="22"><polygon points="12,2 22,22 2,22"/></svg>`,
    chutes: `<svg viewBox="0 0 24 24" fill="#000" height="22" width="22"><path d="M4 4h16v4H4zM4 10h16v4H4zM4 16h16v4H4z"/></svg>`,
    cohere: `<svg viewBox="0 0 24 24" fill="#39594d" height="22" width="22"><circle cx="12" cy="12" r="10"/></svg>`,
    fireworks: `<svg viewBox="0 0 24 24" fill="#e52b2b" height="22" width="22"><path d="M12 2l2 6h6l-5 4 2 6-5-4-5 4 2-6-5-4h6z"/></svg>`,
    glm_china: `<svg viewBox="0 0 24 24" fill="#1a73e8" height="22" width="22"><path d="M4 6h16v2H4zm0 5h16v2H4zm0 5h16v2H4z"/></svg>`,
    glm_coding: `<svg viewBox="0 0 24 24" fill="#1a73e8" height="22" width="22"><path d="M4 6h16v2H4zm0 5h16v2H4zm0 5h16v2H4z"/></svg>`,
    hyperbolic: `<svg viewBox="0 0 24 24" fill="#1a73e8" height="22" width="22"><path d="M4 12c4-8 12-8 16 0-4 8-12 8-16 0z"/></svg>`,
    kimi: `<svg viewBox="0 0 24 24" fill="#000" height="22" width="22"><path d="M6 4h4v16H6zM14 4l-4 8 4 8h4l-4-8 4-8z"/></svg>`,
    minimax_china: `<svg viewBox="0 0 24 24" fill="#ff007f" height="22" width="22"><path d="M2 12l4-8 4 8 4-8 4 8 4-8v16H2z"/></svg>`,
    minimax_coding: `<svg viewBox="0 0 24 24" fill="#ff007f" height="22" width="22"><path d="M2 12l4-8 4 8 4-8 4 8 4-8v16H2z"/></svg>`,
    nebius: `<svg viewBox="0 0 24 24" fill="#00a859" height="22" width="22"><path d="M4 4h4v16H4zM16 4h4v16h-4z"/></svg>`,
    opencode_go: `<svg viewBox="0 0 24 24" fill="#000" height="22" width="22"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M10 16l4-4-4-4" stroke="#fff" stroke-width="2" fill="none"/></svg>`,
    perplexity: `<svg viewBox="0 0 24 24" fill="none" stroke="#000" stroke-width="2" height="22" width="22"><path d="M12 2v20M2 12h20M4.9 4.9l14.2 14.2M4.9 19.1L19.1 4.9"/></svg>`,
    siliconflow: `<svg viewBox="0 0 24 24" fill="#9b51e0" height="22" width="22"><path d="M12 2L2 12l10 10 10-10L12 2zm0 14c-2.21 0-4-1.79-4-4s1.79-4 4-4 4 1.79 4 4-1.79 4-4 4z"/></svg>`,
    together: `<svg viewBox="0 0 24 24" fill="#ff6a00" height="22" width="22"><circle cx="8" cy="12" r="6"/><circle cx="16" cy="12" r="6" fill="#9b51e0"/></svg>`,
    vercel_gateway: `<svg viewBox="0 0 24 24" fill="#000" height="22" width="22"><path d="M12 2L2 22h20L12 2z"/></svg>`,
    vertex_partner: `<svg viewBox="0 0 24 24" fill="#1a73e8" height="22" width="22"><path d="M12 2l10 17H2L12 2zm0 4L6 17h12L12 6z"/></svg>`,
    vertex_ai: `<svg viewBox="0 0 24 24" fill="#1a73e8" height="22" width="22"><path d="M12 2l10 17H2L12 2zm0 4L6 17h12L12 6z"/></svg>`,
    volcengine: `<svg viewBox="0 0 24 24" fill="#1a73e8" height="22" width="22"><path d="M4 22L12 2l8 20H4z"/></svg>`,
    xiaomi_mimo: `<svg viewBox="0 0 24 24" fill="#ff6900" height="22" width="22"><rect x="2" y="2" width="20" height="20" rx="4"/><text x="12" y="16" font-size="12" font-family="sans-serif" font-weight="bold" fill="#fff" text-anchor="middle">MI</text></svg>`,
    xiaomi_mimo_token: `<svg viewBox="0 0 24 24" fill="#ff6900" height="22" width="22"><rect x="2" y="2" width="20" height="20" rx="4"/><text x="12" y="16" font-size="12" font-family="sans-serif" font-weight="bold" fill="#fff" text-anchor="middle">MI</text></svg>`,
    blacksand: `<svg viewBox="0 0 24 24" fill="#0f172a" height="22" width="22"><path d="M12 2l10 6-10 14L2 8l10-6zm0 3.2L5.5 8.4 12 18l6.5-9.6L12 5.2z"/><circle cx="12" cy="9" r="1.6" fill="#f97316"/></svg>`,
    robot: `<svg viewBox="0 0 24 24" width="15" height="15" stroke="currentColor" stroke-width="2" fill="none"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><path d="M8 15h.01M16 15h.01"/></svg>`,
    copy: `<svg viewBox="0 0 24 24" width="13" height="13" stroke="currentColor" stroke-width="2" fill="none"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`,
    link: `<svg viewBox="0 0 24 24" width="13" height="13" stroke="currentColor" stroke-width="2" fill="none"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>`
};

// OAuth flow metadata mirrors app/oauth.py's 9router-compatible registry.
const OAUTH_FLOW_TYPES = {
    'claude': 'authorization_code_pkce',
    'codex': 'authorization_code_pkce',
    'antigravity': 'authorization_code',
    'github': 'device_code',
    'qwen': 'device_code',
    'kiro': 'device_code',
    'grok-cli': 'device_code',
    'cursor': 'import_token',
    'kiro-import': 'import_token',
};

const OAUTH_PROVIDER_CONFIG = {
    codex: { flowType: 'authorization_code_pkce', fixedPort: 1455, callbackPath: '/auth/callback' },
    cursor: { flowType: 'import_token' },
};

// Predefined model lists for OAuth providers — mirrors 9Router's AI_PROVIDERS
// (extracted from 9router/app/.next-cli-build/server/chunks/2573.js).
// OAuth tokens cannot fetch /v1/models, so these must be static.
const OAUTH_PROVIDER_MODELS = {
    claude: [
        { id: 'claude-opus-5' },
        { id: 'claude-fable-5' },
        { id: 'claude-sonnet-5' },
        { id: 'claude-haiku-4-5-20251001' },
    ],
    codex: [
        { id: 'gpt-5.6-sol' },
        { id: 'gpt-5.6-sol-review' },
        { id: 'gpt-5.6-terra' },
        { id: 'gpt-5.6-terra-review' },
        { id: 'gpt-5.6-luna' },
        { id: 'gpt-5.6-luna-review' },
        { id: 'gpt-5.5' },
        { id: 'gpt-5.5-review' },
        { id: 'gpt-5.4' },
        { id: 'gpt-5.4-review' },
        { id: 'gpt-5.4-mini' },
        { id: 'gpt-5.4-mini-review' },
        { id: 'gpt-5.3-codex-spark' },
        { id: 'gpt-5.3-codex-spark-review' },
        { id: 'gpt-5.5-image', kind: 'image' },
        { id: 'gpt-5.4-image', kind: 'image' },
        { id: 'gpt-5.3-image', kind: 'image' },
    ],
    antigravity: [
        { id: 'gemini-3.6-flash-high' },
        { id: 'gemini-3.6-flash-medium' },
        { id: 'gemini-3.6-flash-low' },
        { id: 'gemini-3.5-flash-high' },
        { id: 'gemini-3-flash-agent' },
        { id: 'gemini-3.5-flash-low' },
        { id: 'gemini-3.5-flash-extra-low' },
        { id: 'gemini-pro-agent' },
        { id: 'gemini-3.1-pro-low' },
        { id: 'claude-sonnet-4-6' },
        { id: 'claude-opus-4-6-thinking' },
        { id: 'gpt-oss-120b-medium' },
        { id: 'gemini-3-flash' },
        { id: 'gemini-3.1-flash-image', kind: 'image' },
    ],
    github: [
        { id: 'gpt-5.2' },
        { id: 'gpt-5.2-codex' },
        { id: 'gpt-5.3-codex' },
        { id: 'gpt-5.4' },
        { id: 'gpt-5.4-mini' },
        { id: 'claude-haiku-4.5' },
        { id: 'claude-opus-4.5' },
        { id: 'claude-sonnet-4.5' },
        { id: 'claude-sonnet-4.6' },
        { id: 'claude-opus-4.6' },
        { id: 'claude-opus-4.7' },
        { id: 'gemini-2.5-pro' },
        { id: 'gemini-3-flash-preview' },
        { id: 'gemini-3.1-pro-preview' },
        { id: 'grok-code-fast-1' },
        { id: 'oswe-vscode-prime' },
        { id: 'goldeneye-free-auto' },
        { id: 'text-embedding-3-small', kind: 'embedding' },
        { id: 'text-embedding-3-large', kind: 'embedding' },
    ],
    'grok-cli': [
        { id: 'grok-build' },
        { id: 'grok-4.5' },
        { id: 'grok-4.5-high' },
        { id: 'grok-4.5-medium' },
        { id: 'grok-4.5-low' },
    ],
    kiro: [
        { id: 'claude-opus-5' },
        { id: 'claude-opus-5-thinking' },
        { id: 'claude-opus-5-agentic' },
        { id: 'claude-opus-5-thinking-agentic' },
        { id: 'claude-opus-4.8' },
        { id: 'claude-opus-4.8-thinking' },
        { id: 'claude-opus-4.8-agentic' },
        { id: 'claude-opus-4.8-thinking-agentic' },
        { id: 'claude-opus-4.7' },
        { id: 'claude-opus-4.7-thinking' },
        { id: 'claude-opus-4.7-agentic' },
        { id: 'claude-opus-4.7-thinking-agentic' },
        { id: 'claude-opus-4.5' },
        { id: 'claude-opus-4.5-thinking' },
        { id: 'claude-opus-4.5-agentic' },
        { id: 'claude-opus-4.5-thinking-agentic' },
        { id: 'claude-sonnet-5' },
        { id: 'claude-sonnet-4.5' },
        { id: 'claude-haiku-4.5' },
        { id: 'deepseek-3.2' },
        { id: 'qwen3-coder-next' },
        { id: 'glm-5' },
        { id: 'MiniMax-M2.5' },
        { id: 'gpt-5.6-sol' },
        { id: 'gpt-5.6-terra' },
        { id: 'gpt-5.6-luna' },
        { id: 'claude-sonnet-5-thinking' },
        { id: 'claude-sonnet-4.5-thinking' },
        { id: 'claude-haiku-4.5-thinking' },
        { id: 'gpt-5.6-sol-thinking' },
        { id: 'gpt-5.6-terra-thinking' },
        { id: 'gpt-5.6-luna-thinking' },
        { id: 'claude-sonnet-5-agentic' },
        { id: 'claude-sonnet-4.5-agentic' },
        { id: 'claude-haiku-4.5-agentic' },
        { id: 'gpt-5.6-sol-agentic' },
        { id: 'gpt-5.6-terra-agentic' },
        { id: 'gpt-5.6-luna-agentic' },
        { id: 'claude-sonnet-5-thinking-agentic' },
        { id: 'claude-sonnet-4.5-thinking-agentic' },
        { id: 'claude-haiku-4.5-thinking-agentic' },
        { id: 'gpt-5.6-sol-thinking-agentic' },
        { id: 'gpt-5.6-terra-thinking-agentic' },
        { id: 'gpt-5.6-luna-thinking-agentic' },
    ],
    cursor: [
        { id: 'default' },
        { id: 'claude-4.5-opus-high-thinking' },
        { id: 'claude-4.5-opus-high' },
        { id: 'claude-4.5-sonnet-thinking' },
        { id: 'claude-4.5-sonnet' },
        { id: 'claude-4.5-haiku' },
        { id: 'claude-4.5-opus' },
        { id: 'gpt-5.2-codex' },
        { id: 'claude-4.6-opus-max' },
        { id: 'claude-4.6-sonnet-medium-thinking' },
        { id: 'kimi-k2.5' },
        { id: 'gemini-3-flash-preview' },
        { id: 'gpt-5.2' },
        { id: 'gpt-5.3-codex' },
    ],
};

const KNOWN_PROVIDERS = {
    oauth: [
        // IDs and flow categories mirror app/oauth.py's 9router registry.
        { id: 'claude',      name: 'Claude Code',    format: 'anthropic', url: 'https://api.anthropic.com/v1' },
        { id: 'antigravity', name: 'Antigravity',    format: 'openai',    url: 'https://daily-cloudcode-pa.googleapis.com' },
        { id: 'codex',       name: 'OpenAI Codex',   format: 'openai',    url: 'https://chatgpt.com/backend-api/codex' },
        { id: 'github',      name: 'GitHub Copilot', format: 'openai',    url: 'https://api.githubcopilot.com' },
        { id: 'cursor',      name: 'Cursor IDE',     format: 'openai',    url: 'https://api2.cursor.sh' },
        { id: 'grok-cli',    name: 'Grok CLI',       format: 'openai',    url: 'https://api.x.ai/v1' },
        { id: 'kiro',        name: 'Kiro AI',        format: 'kiro',      url: 'https://runtime.us-east-1.kiro.dev' },
    ],
    api: [
        // Core first-party
        { id: 'anthropic',   name: 'Anthropic',           format: 'anthropic', url: 'https://api.anthropic.com/v1' },
        { id: 'openai',      name: 'OpenAI',              format: 'openai',    url: 'https://api.openai.com/v1' },
        { id: 'azure',       name: 'Azure OpenAI',        format: 'openai',    url: '' },
        { id: 'gemini',      name: 'Gemini',              format: 'gemini',    url: 'https://generativelanguage.googleapis.com/v1beta/openai' },
        { id: 'vertex',      name: 'Vertex AI',           format: 'openai',    url: 'https://aiplatform.googleapis.com' },
        // Aggregator (covers many minor providers via one key)
        { id: 'openrouter',  name: 'OpenRouter',          format: 'openai',    url: 'https://openrouter.ai/api/v1' },
        // Major coding-focused providers
        { id: 'deepseek',    name: 'DeepSeek',            format: 'openai',    url: 'https://api.deepseek.com' },
        // GLM: Coding uses Anthropic wire format; China uses OpenAI
        { id: 'glm',         name: 'GLM Coding',          format: 'anthropic', url: 'https://api.z.ai/api/anthropic/v1' },
        { id: 'glm-cn',      name: 'GLM (China)',         format: 'openai',    url: 'https://open.bigmodel.cn/api/coding/paas/v4' },
        // Kimi/MiniMax use Anthropic messages API
        { id: 'kimi',        name: 'Kimi',                format: 'anthropic', url: 'https://api.kimi.com/coding/v1' },
        { id: 'minimax',     name: 'Minimax Coding',      format: 'anthropic', url: 'https://api.minimax.io/anthropic/v1' },
        { id: 'minimax-cn',  name: 'Minimax (China)',     format: 'anthropic', url: 'https://api.minimaxi.com/anthropic/v1' },
        // Other majors
        { id: 'groq',        name: 'Groq',                format: 'openai',    url: 'https://api.groq.com/openai/v1' },
        { id: 'mistral',     name: 'Mistral',             format: 'openai',    url: 'https://api.mistral.ai/v1' },
        { id: 'commandcode', name: 'Command Code',        format: 'openai',    url: 'https://api.commandcode.ai/alpha' },
        { id: 'xiaomi-mimo', name: 'Xiaomi MiMo',         format: 'openai',    url: 'https://api.xiaomimimo.com/v1' },
        { id: 'opencode-go', name: 'OpenCode Go',         format: 'openai',    url: 'https://opencode.ai/zen/go/v1' },
        // Local inference
        { id: 'ollama',      name: 'Ollama Cloud',        format: 'openai',    url: 'https://ollama.com/api' },
        { id: 'ollama-local',name: 'Ollama Local',        format: 'openai',    url: 'http://localhost:11434/api' }
    ],
    image: [
        { id: 'openai-image',   name: 'OpenAI (DALL-E)',  format: 'openai-image', url: 'https://api.openai.com/v1' },
        { id: 'gemini-image',   name: 'Gemini (Imagen)',  format: 'gemini-image', url: 'https://generativelanguage.googleapis.com/v1beta' },
        { id: 'grok-image',     name: 'Grok (Flux)',      format: 'grok-image',   url: 'https://api.x.ai/v1' },
        { id: 'qwen-image',     name: 'Qwen (Wanx)',      format: 'qwen-image',   url: 'https://dashscope.aliyuncs.com/api/v1' }
    ],
    video: [
        { id: 'openai-video',   name: 'OpenAI (Sora)',    format: 'openai-video', url: 'https://api.openai.com/v1' },
        { id: 'google-veo',     name: 'Google (Veo)',     format: 'gemini-video', url: 'https://generativelanguage.googleapis.com/v1beta' },
        { id: 'grok-video',     name: 'Grok (Video)',     format: 'grok-video',   url: 'https://api.x.ai/v1' },
        { id: 'runway-video',   name: 'Runway (Gen-3)',   format: 'runway-video', url: 'https://api.runwayml.com/v1' }
    ]
};

// ── Blacksand Labs — fixed, built-in BSL model provider ──────────────────────
// This virtual provider surfaces Blacksand products inside the normal model browser.
// Model IDs are intrinsic and bare; each client chooses its own provider prefix.
const BLACKSAND_PROVIDER_ID = 'blacksand';
const BLACKSAND_MODELS = [
    { id: 'blacksand-chat',          name: 'Blacksand Chat',          status: 'active',  desc: 'Category-aware smart routing across the 13×3 matrix' },
    { id: 'blacksand-lite',          name: 'Blacksand Lite',          status: 'active',  desc: 'Coding-agent single-task router (10×3 matrix)' },
    { id: 'blacksand-agentic',       name: 'Blacksand Agentic',       status: 'active',  desc: 'Fast-tier agentic coding orchestration (depth=fast)' },
    { id: 'blacksand-agentic-ultra', name: 'Blacksand Agentic Ultra', status: 'active',  desc: 'Balanced-tier coding orchestration (depth=balanced)' },
    { id: 'blacksand-agentic-max',   name: 'Blacksand Agentic Max',   status: 'active',  desc: 'Multi-domain fusion for Openclaw/Hermes (depth=balanced)' }
];
const BLACKSAND_ACTIVE_IDS = new Set(['blacksand-chat', 'blacksand-lite', 'blacksand-agentic', 'blacksand-agentic-ultra', 'blacksand-agentic-max']);

// Build name cache once KNOWN_PROVIDERS is defined
function _buildNameCache() {
    ['oauth', 'api', 'image', 'video'].forEach(cat => {
        KNOWN_PROVIDERS[cat].forEach(p => {
            _providerNameCache[p.id] = p.name;
        });
    });
    _providerNameCache[BLACKSAND_PROVIDER_ID] = 'Blacksand Labs';
}
_buildNameCache();

// Ensure the fixed Blacksand Labs provider exists in globalConfig with its
// static model list. Idempotent — safe to call on every config load. The
// provider is marked read-only so the detail view hides connection/key editing.
function ensureBlacksandProvider() {
    if (!globalConfig.providers) globalConfig.providers = {};
    const existing = globalConfig.providers[BLACKSAND_PROVIDER_ID] || {};
    globalConfig.providers[BLACKSAND_PROVIDER_ID] = {
        ...existing,
        type: 'bsl',
        format: 'bsl',
        name: 'Blacksand Labs',
        builtin: true,
        readonly: true,
        connections: [],           // no API key / base URL — internal routing
        models: BLACKSAND_MODELS.map(m => ({
            id: m.id,
            name: m.name,
            desc: m.desc,
            status: m.status,
            // Only active families are selectable; planned ones render disabled.
            enabled: BLACKSAND_ACTIVE_IDS.has(m.id)
        }))
    };
}

function _isBlacksandProvider(id) {
    return id === BLACKSAND_PROVIDER_ID;
}

// Only active families are selectable. Canonical intrinsic IDs are sent to the
// router; provider qualification belongs to each client's provider configuration.
function _bslSelectableModels() {
    return BLACKSAND_MODELS.filter(m => BLACKSAND_ACTIVE_IDS.has(m.id));
}

// <optgroup> for <select>-based dropdowns. Rendered directly under the Combo
// Models optgroup so BSL families sit right beneath combos when browsing.
function _bslModelsOptgroupHTML(selectedValue) {
    const models = _bslSelectableModels();
    if (models.length === 0) return '';
    let html = '<optgroup label="BSL Models (Blacksand Labs)">';
    for (const m of models) {
        const sel = selectedValue === m.id ? 'selected' : '';
        html += `<option value="${m.id}" ${sel}>${m.name}</option>`;
    }
    html += '</optgroup>';
    return html;
}

function getDisplayName(id) {
    if (globalConfig.providers && globalConfig.providers[id] && globalConfig.providers[id].name) {
        return globalConfig.providers[id].name;
    }
    return _providerNameCache[id] || id;
}

// ── Admin Auth State ──
// Set by checkAdminAuth() on page load. When true and not authenticated,
// the login overlay is shown and the main UI is hidden.
let _adminAuthRequired = false;
let _adminAuthenticated = false;

/**
 * Check auth status before loading the admin panel.
 * If password protection is enabled and no valid session exists,
 * shows the login overlay and blocks UI initialization.
 */
async function checkAdminAuth() {
    try {
        const res = await fetch('/api/auth/status');
        const data = await res.json();
        _adminAuthRequired = data.auth_required === true;
        _adminAuthenticated = data.authenticated === true;

        if (_adminAuthRequired && !_adminAuthenticated) {
            showLoginOverlay();
            return false;
        }
        hideLoginOverlay();
        return true;
    } catch (err) {
        console.error('Auth status check failed:', err);
        // On error, allow access (fail-open for usability)
        return true;
    }
}

function showLoginOverlay() {
    const overlay = document.getElementById('login-overlay');
    if (overlay) overlay.style.display = 'flex';
    const mainContent = document.querySelector('.app-container');
    if (mainContent) mainContent.style.display = 'none';
    // Focus the password input
    setTimeout(() => {
        const input = document.getElementById('login-password');
        if (input) input.focus();
    }, 100);
}

function hideLoginOverlay() {
    const overlay = document.getElementById('login-overlay');
    if (overlay) overlay.style.display = 'none';
    const mainContent = document.querySelector('.app-container');
    if (mainContent) mainContent.style.display = 'flex';
}

/**
 * Handle login form submission.
 * Called by the login button onclick.
 */
async function handleAdminLogin() {
    const input = document.getElementById('login-password');
    const errorDiv = document.getElementById('login-error');
    const btn = document.getElementById('login-submit-btn');

    if (!input) return;
    const password = input.value;

    if (password.length < 6) {
        if (errorDiv) {
            errorDiv.textContent = 'Password must be at least 6 characters';
            errorDiv.style.display = 'block';
        }
        return;
    }

    if (btn) { btn.disabled = true; btn.textContent = 'Unlocking...'; }
    if (errorDiv) errorDiv.style.display = 'none';

    try {
        const res = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ password }),
        });

        if (res.ok) {
            _adminAuthenticated = true;
            input.value = '';
            if (btn) { btn.disabled = false; btn.textContent = 'Unlock'; }
            hideLoginOverlay();
            // Now load the full UI
            await fetchConfig();
        } else {
            const data = await res.json().catch(() => ({}));
            if (errorDiv) {
                errorDiv.textContent = data.error || 'Invalid password';
                errorDiv.style.display = 'block';
            }
            if (btn) { btn.disabled = false; btn.textContent = 'Unlock'; }
        }
    } catch (err) {
        if (errorDiv) {
            errorDiv.textContent = 'Connection error — is BSL Router running?';
            errorDiv.style.display = 'block';
        }
        if (btn) { btn.disabled = false; btn.textContent = 'Unlock'; }
    }
}

/**
 * Handle logout — clears session and shows login overlay.
 */
async function handleAdminLogout() {
    try {
        await fetch('/api/auth/logout', { method: 'POST' });
    } catch (err) {
        console.error('Logout error:', err);
    }
    _adminAuthenticated = false;
    showLoginOverlay();
    showToast('Logged out — password required to access admin panel');
}

/**
 * Handle shutdown — terminates the BSL Router backend only.
 */
async function handleShutdown() {
    if (!confirm('Are you sure you want to shutdown BSL Router?\n\nThis will stop the backend service. Other applications will not be affected.')) {
        return;
    }
    try {
        const res = await fetch('/api/system/shutdown', { method: 'POST' });
        if (res.ok) {
            showToast('BSL Router is shutting down...');
            // Show a full-screen message since the server is going away
            const content = document.getElementById('main-content');
            if (content) {
                content.innerHTML = `
                    <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:60vh;gap:16px;">
                        <svg viewBox="0 0 24 24" width="48" height="48" stroke="var(--text-muted)" stroke-width="1.5" fill="none"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
                        <h2 style="font-size:20px;font-weight:600;color:var(--text-main);">BSL Router has shut down</h2>
                        <p style="color:var(--text-muted);font-size:14px;">The backend service was stopped. Restart it to access this panel again.</p>
                    </div>`;
            }
        } else {
            showToast('Shutdown failed — check if server is still running', true);
        }
    } catch (err) {
        // Server likely already shut down — that's a success
        showToast('BSL Router has shut down');
    }
}

// ── Anti-Freeze: live stream count polling ──
let _afzPollTimer = null;

async function pollActiveStreams() {
    try {
        const res = await fetch('/api/antifreeze/status');
        if (res.ok) {
            const data = await res.json();
            const el = document.getElementById('afz-active-count');
            if (el) {
                const count = data.active_streams || 0;
                el.textContent = count;
                el.style.color = count > 10 ? 'var(--danger)' : (count > 3 ? '#f59e0b' : 'var(--text-main)');
            }
        }
    } catch (e) {
        const el = document.getElementById('afz-active-count');
        if (el) { el.textContent = '⚠'; el.style.color = 'var(--danger)'; }
    }
}

function startAfzPolling() {
    if (_afzPollTimer) clearInterval(_afzPollTimer);
    pollActiveStreams();
    _afzPollTimer = setInterval(pollActiveStreams, 5000);
}

function stopAfzPolling() {
    if (_afzPollTimer) { clearInterval(_afzPollTimer); _afzPollTimer = null; }
}

async function handleForceStopStreams() {
    if (!confirm('Force-stop all active streams?\n\nEach cancelled stream will receive an error + [DONE] frame so the client unblocks. This does NOT restart the router.')) return;
    try {
        const res = await fetch('/api/antifreeze/force-stop', { method: 'POST' });
        const data = await res.json();
        showToast(`Cancelled ${data.cancelled || 0} active stream(s).`, false);
        pollActiveStreams();
    } catch (e) {
        showToast('Force-stop failed — router may be unresponsive', true);
    }
}

function toggleAutoRestart(enabled) {
    if (!globalConfig.watchdog) globalConfig.watchdog = {};
    globalConfig.watchdog.auto_restart = enabled;
    scheduleAutoSave();
    showToast(enabled ? 'Auto-restart enabled — restart router to activate watchdog' : 'Auto-restart disabled');
}

// Enter key support for login password field
document.addEventListener('DOMContentLoaded', () => {
    // BSL matrix auto-marked select styling — injected here because style.css
    // is not part of this task's allowed write paths.
    const bslAutoMarkedStyle = document.createElement('style');
    bslAutoMarkedStyle.textContent =
        '.bsl-slot-select.auto-marked { border-color: #a78bfa; background-color: #f5f3ff; font-weight: 600; color: #7c3aed; ' +
        'background-image: url("data:image/svg+xml;charset=UTF-8,%3csvg xmlns=\'http://www.w3.org/2000/svg\' width=\'12\' height=\'12\' viewBox=\'0 0 24 24\' fill=\'none\' stroke=\'%237c3aed\' stroke-width=\'2\'%3e%3cpolyline points=\'6 9 12 15 18 9\'/%3e%3c/svg%3e"); }';
    document.head.appendChild(bslAutoMarkedStyle);

    const loginInput = document.getElementById('login-password');
    if (loginInput) {
        loginInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') handleAdminLogin();
        });
    }
    document.getElementById('conn-url')?.addEventListener('blur', () => {
        const format = globalConfig.providers?.[activeProviderId]?.format || 'openai';
        normalizeUrlField('conn-url', 'conn-url-notice', format, true);
    });
    document.getElementById('p-url')?.addEventListener('blur', () => {
        normalizeUrlField('p-url', 'p-url-notice', document.getElementById('p-format')?.value, false);
    });
    document.getElementById('p-format')?.addEventListener('change', () => {
        updateCustomUrlHelp();
        normalizeUrlField('p-url', 'p-url-notice', document.getElementById('p-format')?.value, false);
    });
});

async function fetchConfig() {
    try {
        const res = await fetch('/api/config');
        globalConfig = await res.json();
        if (!globalConfig.providers) globalConfig.providers = {};
        if (!globalConfig.tools) globalConfig.tools = {};
        ['oauth', 'api', 'image', 'video'].forEach(cat => {
            KNOWN_PROVIDERS[cat].forEach(p => {
                if (!globalConfig.providers[p.id]) {
                    globalConfig.providers[p.id] = { type: cat, format: p.format, connections: [], models: [] };
                } else {
                    // Always enforce type from KNOWN_PROVIDERS — prevents stale config from mis-routing oauth providers
                    globalConfig.providers[p.id].type = cat;
                }
            });
        });

        // Auto-populate predefined models for OAuth providers (mirrors 9Router's PROVIDER_MODELS).
        // OAuth tokens can't fetch /v1/models, so these must be static.
        // Merge strategy: seed if empty, and backfill any missing predefined models
        // so new additions (e.g. image models) propagate to existing configs.
        KNOWN_PROVIDERS.oauth.forEach(p => {
            const prov = globalConfig.providers[p.id];
            const predefined = OAUTH_PROVIDER_MODELS[p.id];
            if (prov && predefined) {
                if (!prov.models || prov.models.length === 0) {
                    prov.models = predefined.map(m => ({
                        id: m.id,
                        name: m.id,
                        enabled: true
                    }));
                } else {
                    // Backfill: add any predefined models not already in the config
                    const existingIds = new Set(prov.models.map(m => m.id));
                    predefined.forEach(m => {
                        if (!existingIds.has(m.id)) {
                            prov.models.push({
                                id: m.id,
                                name: m.id,
                                enabled: true
                            });
                        }
                    });
                }
            }
        });

        // Migrate legacy api_key/base_url to connections array
        for (let key in globalConfig.providers) {
            let p = globalConfig.providers[key];
            if (!p.connections) p.connections = [];
            if (p.api_key && p.connections.length === 0) {
                p.connections.push({ name: 'Primary Connection', base_url: p.base_url || '', api_key: p.api_key, enabled: true });
                delete p.api_key;
                delete p.base_url;
            }
        }

        // Seed the fixed Blacksand Labs provider (BSL model family) so it shows
        // up in the provider list and every model browser, always in sync with
        // the built-in catalog.
        ensureBlacksandProvider();

        setupAutoClearInterval();
        renderActiveTab();
    } catch (err) { console.error('Failed to load config', err); }
}

// ── Mobile sidebar toggle ───────────────────────────────────────────────────
function toggleSidebar() {
    const sidebar = document.querySelector('.sidebar');
    const backdrop = document.getElementById('sidebar-backdrop');
    if (!sidebar || !backdrop) return;
    sidebar.classList.toggle('open');
    backdrop.classList.toggle('show');
}

function closeSidebar() {
    const sidebar = document.querySelector('.sidebar');
    const backdrop = document.getElementById('sidebar-backdrop');
    if (!sidebar || !backdrop) return;
    sidebar.classList.remove('open');
    backdrop.classList.remove('show');
}

// Auto-close sidebar when a nav item is clicked (mobile UX)
document.addEventListener('click', function(e) {
    const navItem = e.target.closest('.nav-item');
    if (navItem) {
        // Small delay to allow tab switch to register
        setTimeout(() => closeSidebar(), 150);
    }
});

// Close sidebar on resize to desktop
window.addEventListener('resize', function() {
    if (window.innerWidth > 768) {
        closeSidebar();
    }
});

// ── Auto-Save Infrastructure ──────────────────────────────────────────────────
// Debounced save: collects rapid changes and fires one POST after 500ms of
// inactivity. The save indicator (#save-indicator) flashes "Saving…" → "Saved ✓"
// so the user always knows the state without clicking a button.
let _autoSaveTimer = null;
let _autoSaveInFlight = false;

async function saveConfig() {
    if (_autoSaveInFlight) return false;
    _autoSaveInFlight = true;
    _setSaveIndicator('saving');
    try {
        const res = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(globalConfig)
        });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            showToast(`Configuration save failed: ${data.error || res.statusText || 'request failed'}`, true);
            _setSaveIndicator('error');
            return false;
        }
        _setSaveIndicator('saved');
        return true;
    } catch (err) {
        showToast(`Configuration save failed: ${err.message || 'network error'}`, true);
        _setSaveIndicator('error');
        return false;
    } finally {
        _autoSaveInFlight = false;
    }
}

function scheduleAutoSave() {
    if (_autoSaveTimer) clearTimeout(_autoSaveTimer);
    _setSaveIndicator('pending');
    _autoSaveTimer = setTimeout(() => {
        _autoSaveTimer = null;
        saveConfig();
    }, 500);
}

function _setSaveIndicator(state) {
    const el = document.getElementById('save-indicator');
    if (!el) return;
    const map = {
        idle:    { text: '',             cls: 'save-idle' },
        pending: { text: 'Editing…',     cls: 'save-pending' },
        saving:  { text: 'Saving…',      cls: 'save-saving' },
        saved:   { text: 'Saved ✓',      cls: 'save-saved' },
        error:   { text: 'Save failed',  cls: 'save-error' },
    };
    const s = map[state] || map.idle;
    el.textContent = s.text;
    el.className = 'save-indicator ' + s.cls;
    if (state === 'saved' || state === 'error') {
        setTimeout(() => { if (el.textContent === s.text) { el.textContent = ''; el.className = 'save-indicator save-idle'; } }, 2000);
    }
}

// Global change listener: any input change inside #main-content triggers
// debounced auto-save. This catches all Tools/EP/Settings tab handlers
// without needing to modify each onchange attribute individually.
// The _autoSaveInFlight guard prevents double-saves when an onchange
// handler also calls saveGlobalConfig() directly.
document.addEventListener('change', function(e) {
    if (!e.target.closest('#main-content')) return;
    scheduleAutoSave();
});

function showProviderDetail(id) {
    activeProviderId = id;
    currentView = 'detail';
    renderActiveTab();
}

window.backToList = () => {
    currentView = 'list';
    activeProviderId = null;
    renderActiveTab();
};

window.toggleVisibilityMode = () => {
    isEditingVisibility = !isEditingVisibility;
    renderActiveTab();
};

window.saveVisibility = () => {
    document.querySelectorAll('.p-visibility-checkbox').forEach(cb => {
        const id = cb.dataset.id;
        if (!globalConfig.providers[id]) {
            // Search all categories (oauth, api, image, video) to find format
            let cat = null, fmt = null;
            for (const c of ['oauth', 'api', 'image', 'video']) {
                const found = KNOWN_PROVIDERS[c].find(p => p.id === id);
                if (found) { cat = c; fmt = found.format; break; }
            }
            if (!cat) return; // Unknown provider — skip
            globalConfig.providers[id] = { type: cat, format: fmt, connections: [], models: [] };
        }
        globalConfig.providers[id].hidden = cb.checked;
    });
    isEditingVisibility = false;
    saveConfig();
    renderActiveTab();
};

window.toggleProviderCheckbox = (id) => {
    if (!isEditingVisibility) return;
    const cb = document.querySelector(`.p-visibility-checkbox[data-id="${id}"]`);
    if (cb) cb.checked = !cb.checked;
};

function isProviderSelectable(provData) {
    // Hidden providers stay in /api/config for admin edit, but must not appear in selection lists.
    return !!(provData && !provData.hidden);
}

function renderProviderList() {
    let customTextCards = '';
    let customImageCards = '';
    let customVideoCards = '';
    
    for (const [key, p] of Object.entries(globalConfig.providers)) {
        if (p.type === 'custom' || p.type === 'image_custom' || p.type === 'video_custom') {
            if (p.hidden && !isEditingVisibility) continue;
            const isActive = p.connections && p.connections.length > 0;
            const card = providerCard(key, getDisplayName(key), SVGS[key] || letterIcon(key), isActive, `showProviderDetail('${key}')`);
            
            if (p.type === 'image_custom') customImageCards += card;
            else if (p.type === 'video_custom') customVideoCards += card;
            else customTextCards += card;
        }
    }

    let html = `
    <div class="provider-section">
        <h3>Custom Text Providers</h3>
        <div class="provider-grid">
            <div class="provider-card provider-card-add" onclick="openProviderModal('text')" style="${isEditingVisibility ? 'opacity:0.5;pointer-events:none;' : ''}">
                <div class="p-icon-box" style="border:none;background:transparent;font-size:24px;color:var(--brand-color)">+</div>
                <div class="p-info">
                    <div class="p-name" style="color:var(--brand-color)">Add Custom Provider</div>
                    <div class="p-status" style="color:var(--text-muted);font-size:11px;">OpenAI · Anthropic · Gemini</div>
                </div>
            </div>
            ${customTextCards}
        </div>
    </div>`;

    // ── Blacksand Labs (built-in BSL model family) ──
    // A fixed, always-present provider. No connection/API-key — its models
    // route internally through the BSL Router matrix/preset dispatchers.
    {
        const bslCfg = globalConfig.providers[BLACKSAND_PROVIDER_ID] || {};
        const activeCount = _bslSelectableModels().length;
        if (!(bslCfg.hidden && !isEditingVisibility)) {
            html += `
    <div class="provider-section">
        <h3>Blacksand Labs</h3>
        <div class="provider-grid">
            ${providerCard(BLACKSAND_PROVIDER_ID, 'Blacksand Labs', SVGS.blacksand || letterIcon('B'), activeCount > 0, `showProviderDetail('${BLACKSAND_PROVIDER_ID}')`)}
        </div>
    </div>`;
        }
    }

    html += renderSection('OAuth Providers', 'oauth');
    
    // API Providers without the "Test All" button
    html += `
    <div class="provider-section">
        <h3>API Key Providers</h3>
        <div class="provider-grid">`;
    KNOWN_PROVIDERS.api.forEach(p => {
        const cfg = globalConfig.providers[p.id] || {};
        if (cfg.hidden && !isEditingVisibility) return;
        const isActive = cfg.connections && cfg.connections.length > 0;
        html += providerCard(p.id, p.name, SVGS[p.id] || letterIcon(p.id), isActive, `showProviderDetail('${p.id}')`);
    });
    html += `</div></div>`;
    
    html += renderSection('Image Providers', 'image', customImageCards, 'image');
    html += renderSection('Video Providers', 'video', customVideoCards, 'video');
    
    return html;
}

function renderSection(title, category, customCards = '', addType = null) {
    let html = `<div class="provider-section"><h3>${title}</h3><div class="provider-grid">`;
    
    if (addType) {
        html += `
            <div class="provider-card provider-card-add" onclick="openProviderModal('${addType}')" style="${isEditingVisibility ? 'opacity:0.5;pointer-events:none;' : ''}">
                <div class="p-icon-box" style="border:none;background:transparent;font-size:24px;color:var(--brand-color)">+</div>
                <div class="p-info">
                    <div class="p-name" style="color:var(--brand-color)">Add Custom ${addType === 'image' ? 'Image' : 'Video'} Provider</div>
                    <div class="p-status" style="color:var(--text-muted);font-size:11px;">Proxy / Custom Endpoint</div>
                </div>
            </div>
        `;
    }
    
    if (customCards) html += customCards;
    
    KNOWN_PROVIDERS[category].forEach(p => {
        const cfg = globalConfig.providers[p.id] || {};
        if (cfg.hidden && !isEditingVisibility) return;
        const isActive = cfg.connections && cfg.connections.length > 0;
        html += providerCard(p.id, p.name, SVGS[p.id] || letterIcon(p.id), isActive, `showProviderDetail('${p.id}')`);
    });
    html += `</div></div>`;
    return html;
}

function renderProviderFormatBadge(format) {
    const f = String(format || '').toLowerCase();
    let label = 'API', color = '#64748b', bg = '#f1f5f9', border = '#cbd5e1';
    if (f.includes('anthropic')) { label = 'ANT'; color = '#8b5cf6'; bg = '#f5f3ff'; border = '#ddd6fe'; }
    else if (f.includes('gemini')) { label = 'GEM'; color = '#2563eb'; bg = '#eff6ff'; border = '#bfdbfe'; }
    else if (f.includes('openai')) { label = 'OAI'; color = '#059669'; bg = '#ecfdf5'; border = '#a7f3d0'; }
    return `<span title="${format || 'custom'} compatible" style="font-size:9px;line-height:1;font-weight:800;letter-spacing:0.35px;color:${color};background:${bg};border:1px solid ${border};border-radius:6px;padding:3px 5px;flex-shrink:0;">${label}</span>`;
}

function providerCard(id, name, iconHtml, isActive, onclick) {
    const p = globalConfig.providers[id] || {};
    const isOAuth = p.type === 'oauth';
    let statusText;
    if (isOAuth) {
        const tokenCount = (p.connections && p.connections.length) || 0;
        statusText = tokenCount > 0 ? `${tokenCount} Token${tokenCount > 1 ? 's' : ''} Connected` : 'Not Connected';
    } else {
        const count = (p.connections && p.connections.length) || 0;
        statusText = count === 1 ? '1 Connection' : (count > 1 ? count + ' Connections' : 'No connections');
    }
    const activeModelCount = Array.isArray(p.models) ? p.models.filter(model => model && model.enabled !== false).length : 0;
    const modelStatusText = `${activeModelCount} active model${activeModelCount === 1 ? '' : 's'}`;
    
    let checkboxHtml = '';
    if (isEditingVisibility) {
        checkboxHtml = `<input type="checkbox" class="p-visibility-checkbox" data-id="${id}" ${p.hidden ? 'checked' : ''} onclick="event.stopPropagation()">`;
    }
    
    const clickAction = isEditingVisibility ? `toggleProviderCheckbox('${id}')` : onclick;
    
    return `
    <div class="provider-card" onclick="${clickAction}" style="position:relative; ${isEditingVisibility ? 'cursor:pointer;' : ''}">
        ${checkboxHtml ? `<div style="position:absolute; top:12px; right:12px;">${checkboxHtml}</div>` : ''}
        <div class="p-icon-box" style="background:transparent;border:none;">${iconHtml}</div>
        <div class="p-info">
            <div class="p-name" style="display:flex;align-items:center;gap:6px;min-width:0;">
                <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${name}</span>
                ${p.type === 'custom' ? renderProviderFormatBadge(p.format) : ''}
            </div>
            <div class="p-status">
                <div class="status-dot ${isActive ? 'active' : ''}"></div>
                <span class="status-text ${isActive ? 'active' : ''}">${statusText}</span>
            </div>
            ${isActive ? `<div class="p-status" style="padding-left:10px;"><span class="status-text">${modelStatusText}</span></div>` : ''}
        </div>
    </div>`;
}

function letterIcon(id) {
    return `<span style="font-weight:700;font-size:18px;color:var(--text-muted)">${id.charAt(0).toUpperCase()}</span>`;
}

// Read-only detail view for the built-in Blacksand Labs provider. Unlike
// external providers there is no connection/API-key management — the BSL model
// family is served internally by the router's own dispatchers.
window.openBslMatrix = function(modelId) {
    _activeBslFamily = modelId;
    selectTabByName('bsl-models');
};

window.toggleBlacksandModel = function(modelId, enabled) {
    if (modelId === 'blacksand-lite') _setBslLiteEnabled(enabled);
    if (modelId === 'blacksand-agentic') _setBslAgenticEnabled(enabled);
    if (modelId === 'blacksand-agentic-ultra') _setBslAgenticUltraEnabled(enabled);
    if (modelId === 'blacksand-agentic-max') _setBslAgenticMaxEnabled(enabled);
};

function renderBlacksandDetail() {
    const svgIcon = SVGS.blacksand || letterIcon('B');
    const activeCount = _bslSelectableModels().length;
    const statusColor = 'var(--brand-color)';

    const modelRows = BLACKSAND_MODELS.map(m => {
        const isLive = m.status === 'active';
        const familyCfg = m.id === 'blacksand-chat' ? _getBslChatCfg()
            : m.id === 'blacksand-lite' ? _getBslLiteCfg()
            : m.id === 'blacksand-agentic' ? _getBslAgenticCfg()
            : m.id === 'blacksand-agentic-ultra' ? _getBslAgenticUltraCfg()
            : m.id === 'blacksand-agentic-max' ? _getBslAgenticMaxCfg()
            : null;
        const isEnabled = isLive && familyCfg?.enabled === true;
        const badge = isLive
            ? `<span style="font-size:11px;font-weight:700;color:${isEnabled ? 'var(--success)' : 'var(--text-muted)'};text-transform:uppercase;">${isEnabled ? 'On' : 'Off'}</span>`
            : `<span style="font-size:11px;font-weight:600;color:var(--text-muted);text-transform:uppercase;">Soon</span>`;
        const controls = isLive
            ? `<button class="btn btn-outline" style="font-size:11px;padding:4px 10px;" onclick="openBslMatrix('${m.id}')">Open Matrix</button>
               <label class="switch switch-sm" title="Enable/Disable ${m.name}">
                   <input type="checkbox" ${isEnabled ? 'checked' : ''} onchange="toggleBlacksandModel('${m.id}', this.checked)">
                   <span class="slider"></span>
               </label>`
            : '';
        return `
        <div class="connection-row" style="align-items:center;">
            <div style="min-width:0;display:grid;gap:3px;">
                <div style="font-size:13px;color:var(--text-main);font-weight:700;">${m.name}</div>
                <div style="font-size:11px;color:var(--text-muted);">Model ID: <code>${m.id}</code></div>
                <div style="font-size:11px;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${m.desc || ''}</div>
            </div>
            <div style="display:flex;align-items:center;gap:16px;flex-shrink:0;">
                ${controls}
                ${badge}
            </div>
        </div>`;
    }).join('');

    return `
    <div class="detail-hero">
        <div class="detail-hero-left">
            <div class="p-icon-box" style="width:52px;height:52px;background:transparent;border:1px solid var(--border-color);">${svgIcon}</div>
            <div>
                <h1 class="detail-title">Blacksand Labs</h1>
                <div class="detail-conn-count">${activeCount} active model${activeCount !== 1 ? 's' : ''} · built-in</div>
            </div>
        </div>
        <div class="detail-hero-right">
            <span style="font-size:12px;font-weight:600;color:${statusColor};background:var(--brand-light);padding:6px 12px;border-radius:8px;">Internal Routing</span>
        </div>
    </div>

    <div class="detail-card" style="border-radius:12px;border:1px solid var(--border-color);margin-bottom:24px;">
        <div class="detail-card-header">
            <div>
                <h2 style="font-size:15px;">BSL Model Family</h2>
                <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">
                    These models are served by BSL Router itself. No base URL or API key is required —
                    requests are resolved internally through the routing matrix and presets.
                </div>
            </div>
        </div>
        <div style="display:flex;flex-direction:column;gap:2px;margin-top:8px;">
            ${modelRows}
        </div>
    </div>

    <div class="detail-card" style="border-radius:12px;border:1px solid var(--border-color);">
        <div class="detail-card-header">
            <div>
                <h2 style="font-size:15px;">How to use</h2>
                <div style="font-size:12px;color:var(--text-muted);margin-top:4px;line-height:1.6;">
                    Select a BSL model anywhere you pick a model — it appears right under the
                    <strong>Combo Models</strong> group as <strong>BSL Models</strong>. Configure
                    <code style="background:var(--bg-body);padding:1px 5px;border-radius:4px;">bsl-chat</code>
                    routing in the <strong>BSL Models</strong> tab.
                </div>
            </div>
        </div>
    </div>`;
}

function renderProviderDetail() {
    const p = globalConfig.providers[activeProviderId];
    if (!p) return '<div>Provider not found</div>';

    // ── Blacksand Labs — built-in BSL family, read-only detail ──
    // No connections / API keys. Just show the model family and status.
    if (_isBlacksandProvider(activeProviderId)) {
        return renderBlacksandDetail();
    }

    const models = p.models || [];
    const svgIcon = SVGS[activeProviderId] || letterIcon(activeProviderId);
    const isCustom = p.type === 'custom' || p.type === 'image_custom' || p.type === 'video_custom';
    const displayName = getDisplayName(activeProviderId);
    const isOAuth = p.type === 'oauth';
    const connCount = p.connections ? p.connections.length : 0;
    const oauthStatusText = connCount > 0 ? `${connCount} Token${connCount > 1 ? 's' : ''} Connected` : 'Not Connected';

    return `
    <div class="detail-hero">
        <div class="detail-hero-left">
            <div class="p-icon-box" style="width:52px;height:52px;background:transparent;border:1px solid var(--border-color);">${svgIcon}</div>
            <div>
                <h1 class="detail-title">${displayName}</h1>
                <div class="detail-conn-count">${isOAuth ? oauthStatusText : `${connCount} connection${connCount !== 1 ? 's' : ''}`}</div>
            </div>
        </div>
        <div class="detail-hero-right">
            ${!isCustom && !isOAuth ? `<a href="#" class="get-api-key-link">ðŸ”‘ Get API Key ${SVGS.link}</a>` : ''}
            ${isCustom ? `<button class="btn btn-danger" onclick="deleteActiveProvider()">Delete Provider</button>` : ''}
        </div>
    </div>

    ${isOAuth ? `
    <div class="detail-card">
        <div class="detail-card-header">
            <h2>${OAUTH_FLOW_TYPES[activeProviderId] === 'device_code' ? 'Device Code Authentication' : 'OAuth Integration'}</h2>
            ${connCount > 0 ? `<span style="font-size:12px;font-weight:600;color:var(--success);">${oauthStatusText}</span>` : ''}
        </div>
        ${connCount > 0 ? p.connections.map((conn, idx) => `
        <div class="connection-row" style="padding:12px 16px; border:1px solid var(--border-color); border-radius:8px; display:flex; align-items:center; justify-content:space-between; margin-bottom:8px;">
            <div style="display:flex; align-items:center; gap:12px;">
                <div style="display:flex; flex-direction:column; gap:4px;">
                    <button class="btn btn-outline" style="padding:2px 6px; font-size:10px; line-height:1; display:flex; align-items:center; justify-content:center; border:none; background:transparent; cursor:pointer;" onclick="moveConnectionUp(${idx})" ${idx === 0 ? 'disabled style="opacity:0.3; cursor:not-allowed;"' : ''}>
                        <svg viewBox="0 0 24 24" width="12" height="12" stroke="currentColor" stroke-width="2.5" fill="none"><polyline points="18 15 12 9 6 15"/></svg>
                    </button>
                    <button class="btn btn-outline" style="padding:2px 6px; font-size:10px; line-height:1; display:flex; align-items:center; justify-content:center; border:none; background:transparent; cursor:pointer;" onclick="moveConnectionDown(${idx})" ${idx === connCount - 1 ? 'disabled style="opacity:0.3; cursor:not-allowed;"' : ''}>
                        <svg viewBox="0 0 24 24" width="12" height="12" stroke="currentColor" stroke-width="2.5" fill="none"><polyline points="6 9 12 15 18 15"/></svg>
                    </button>
                </div>
                <div>
                    <div class="conn-name">${conn.name || 'Token ' + (idx + 1)}</div>
                    <div class="conn-key" style="font-size:11px;color:var(--text-muted);">Expires: ${conn.expires_at ? new Date(conn.expires_at).toLocaleString() : 'Session-based'}</div>
                    <div style="display:flex; align-items:center; gap:8px; margin-top:4px;">
                        <span style="display:flex; align-items:center; gap:4px; font-size:11px; font-weight:600; color:${conn.enabled !== false ? 'var(--success)' : 'var(--text-muted)'}; background:${conn.enabled !== false ? '#ecfdf5' : '#f3f4f6'}; padding:2px 6px; border-radius:12px;">
                            <div style="width:6px;height:6px;background:${conn.enabled !== false ? 'var(--success)' : 'var(--text-muted)'};border-radius:50%;"></div> ${conn.enabled !== false ? 'active' : 'disabled'}
                        </span>
                        <span style="font-size:11px; background:#f3f4f6; color:var(--text-muted); padding:2px 6px; border-radius:4px; font-weight:500;">OAuth</span>
                        <span style="font-size:11px; color:var(--text-muted);">#${idx + 1}</span>
                    </div>
                </div>
            </div>
            <div style="display:flex; align-items:center; gap:16px;">
                <div style="display:flex; align-items:center; gap:12px;">
                    <div style="display:flex; flex-direction:column; align-items:center; cursor:pointer; color:var(--danger);" onclick="deleteConnection(${idx})">
                        <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                        <span style="font-size:10px; margin-top:2px;">Revoke</span>
                    </div>
                </div>
                <label class="switch"><input type="checkbox" ${conn.enabled !== false ? 'checked' : ''} onchange="toggleConnection(${idx}, this.checked)"><span class="slider" style="${conn.enabled !== false ? 'background:var(--brand-color)' : ''}"></span></label>
            </div>
        </div>
        `).join('') : ''}
        <div style="padding: ${connCount > 0 ? '16px 0 8px' : '24px 0'}; display:flex; flex-direction:column; align-items:center; gap: 16px;">
            ${connCount === 0 ? `<div style="width: 48px; height: 48px; color: var(--text-main);">${svgIcon}</div>
            <div style="font-size: 14px; color: var(--text-muted); text-align:center;">
                ${OAUTH_FLOW_TYPES[activeProviderId] === 'device_code' ? `Authenticate with ${displayName} using a device code — no API key needed.` : `Connect your ${displayName} account to import context securely without API keys.`}
            </div>` : ''}
            <div style="display:flex;gap:12px;">
                <button class="btn btn-primary" onclick="openOAuthTokenModal()" style="font-size:14px; padding: 10px 24px;">
                    ${connCount > 0 ? '+ Add Another Token' : `Connect with ${displayName}`}
                </button>
                ${connCount > 0 ? '' : ''}
            </div>
        </div>
    </div>
    ` : `
    <div class="detail-card" style="border-radius:12px;border:1px solid var(--border-color);margin-bottom:24px;">
        <div class="detail-card-header" style="display:flex; justify-content:space-between; align-items:center; border-bottom:0; padding-bottom:0;">
            <div>
                <h2 style="font-size:15px;">${p.format === 'anthropic' ? 'Anthropic' : (p.format === 'gemini' ? 'Gemini' : 'OpenAI')} Compatible Details</h2>
                <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">Messages API · ${p.connections && p.connections[0] ? p.connections[0].base_url : ''}</div>
            </div>
            <div style="display:flex; gap:8px;">
                ${isCustom ? `<button class="btn btn-outline" style="padding:6px 12px;display:flex;align-items:center;gap:4px;" onclick="openProviderModal(null, activeProviderId)"><svg viewBox="0 0 24 24" width="13" height="13" stroke="currentColor" stroke-width="2" fill="none"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg> Edit</button>` : ''}
                ${isCustom ? `<button class="btn btn-outline" style="padding:6px 12px;display:flex;align-items:center;gap:4px;" onclick="deleteActiveProvider()"><svg viewBox="0 0 24 24" width="13" height="13" stroke="currentColor" stroke-width="2" fill="none"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg> Delete</button>` : ''}
            </div>
        </div>
    </div>

    <div class="detail-card" style="border-radius:12px;border:1px solid var(--border-color);margin-bottom:24px;">
        <div class="detail-card-header" style="display:flex; justify-content:space-between; align-items:center; padding-bottom:12px;">
            <h2 style="font-size:15px;">Connections</h2>
            <div style="display:flex;align-items:center;gap:12px;">
                <button class="btn btn-primary" style="padding:6px 12px;" onclick="openConnModal()">+ Add API Key</button>
                <button class="btn btn-outline" style="font-size:12px;font-weight:600;padding:4px 12px;display:flex;align-items:center;gap:6px;">
                    <svg viewBox="0 0 24 24" width="13" height="13" stroke="currentColor" stroke-width="2" fill="none"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
                    Test Connection One-by-One
                </button>
                <div style="display:flex;align-items:center;gap:6px;">
                    <span style="font-size:12px;font-weight:500;color:var(--text-muted);">Round Robin</span>
                    <label class="switch"><input type="checkbox" ${p.round_robin ? 'checked' : ''} onchange="toggleProviderRoundRobin(this.checked)"><span class="slider" style="${p.round_robin ? 'background:var(--brand-color)' : ''}"></span></label>
                </div>
            </div>
        </div>
        ${connCount > 0 ? p.connections.map((conn, idx) => `
        <div class="connection-row" style="padding:12px 16px; border:1px solid var(--border-color); border-radius:8px; display:flex; align-items:center; justify-content:space-between; margin-bottom:8px;">
            <div style="display:flex; align-items:center; gap:12px;">
                <svg viewBox="0 0 24 24" width="16" height="16" stroke="var(--text-muted)" stroke-width="2" fill="none" style="transform:rotate(135deg);"><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"></path></svg>
                <div style="display:flex; flex-direction:column;">
                    <div style="font-size:13px; font-weight:600;">${conn.name || 'Connection ' + (idx + 1)}</div>
                    <div style="display:flex; align-items:center; gap:8px; margin-top:4px;">
                        <span style="display:flex; align-items:center; gap:4px; font-size:11px; font-weight:600; color:${conn.enabled !== false ? 'var(--success)' : 'var(--text-muted)'}; background:${conn.enabled !== false ? '#ecfdf5' : '#f3f4f6'}; padding:2px 6px; border-radius:12px;">
                            <div style="width:6px;height:6px;background:${conn.enabled !== false ? 'var(--success)' : 'var(--text-muted)'};border-radius:50%;"></div> ${conn.enabled !== false ? 'active' : 'disabled'}
                        </span>
                        <span style="font-size:11px; background:#f3f4f6; color:var(--text-muted); padding:2px 6px; border-radius:4px; font-weight:500;">API Key</span>
                        ${!conn.api_key ? `<span style="font-size:11px; color:var(--text-muted); font-style:italic;">No API Key set</span>` : ''}
                        ${conn.proxy_url ? `<span style="font-size:11px; background:#fff7ed; color:#c2410c; padding:2px 6px; border-radius:4px; font-weight:500;" title="${conn.proxy_url}">Proxy</span>` : ''}
                        <span style="font-size:11px; color:var(--text-muted);">#${idx + 1}</span>
                    </div>
                </div>
            </div>
            <div style="display:flex; align-items:center; gap:16px;">
                <div style="display:flex; flex-direction:column; gap:4px; margin-right:8px;">
                    <button class="btn btn-outline" style="padding:2px 6px; font-size:10px; line-height:1; display:flex; align-items:center; justify-content:center; border:none; background:transparent; cursor:pointer;" onclick="moveConnectionUp(${idx})" ${idx === 0 ? 'disabled style="opacity:0.3; cursor:not-allowed;"' : ''}>
                        <svg viewBox="0 0 24 24" width="12" height="12" stroke="currentColor" stroke-width="2.5" fill="none"><polyline points="18 15 12 9 6 15"/></svg>
                    </button>
                    <button class="btn btn-outline" style="padding:2px 6px; font-size:10px; line-height:1; display:flex; align-items:center; justify-content:center; border:none; background:transparent; cursor:pointer;" onclick="moveConnectionDown(${idx})" ${idx === connCount - 1 ? 'disabled style="opacity:0.3; cursor:not-allowed;"' : ''}>
                        <svg viewBox="0 0 24 24" width="12" height="12" stroke="currentColor" stroke-width="2.5" fill="none"><polyline points="6 9 12 15 18 15"/></svg>
                    </button>
                </div>
                <div style="display:flex; align-items:center; gap:12px;">
                    <div style="display:flex; flex-direction:column; align-items:center; cursor:pointer; color:var(--text-muted);" onclick="openConnModal(${idx})">
                        <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                        <span style="font-size:10px; margin-top:2px;">Edit</span>
                    </div>
                    <div style="display:flex; flex-direction:column; align-items:center; cursor:pointer; color:var(--danger);" onclick="deleteConnection(${idx})">
                        <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                        <span style="font-size:10px; margin-top:2px;">Delete</span>
                    </div>
                </div>
                <label class="switch"><input type="checkbox" ${conn.enabled !== false ? 'checked' : ''} onchange="toggleConnection(${idx}, this.checked)"><span class="slider" style="${conn.enabled !== false ? 'background:var(--brand-color)' : ''}"></span></label>
            </div>
        </div>
        `).join('') : `
        <div class="empty-state">
            <svg viewBox="0 0 24 24" width="16" height="16" stroke="var(--text-muted)" stroke-width="2" fill="none"><path d="M18 8h1a4 4 0 0 1 0 8h-1"/><path d="M2 8h16v9a4 4 0 0 1-4 4H6a4 4 0 0 1-4-4V8z"/><line x1="6" y1="1" x2="6" y2="4"/><line x1="10" y1="1" x2="10" y2="4"/><line x1="14" y1="1" x2="14" y2="4"/></svg>
            No connections yet
        </div>`}
    </div>
    `}

    <div class="detail-card" style="border-radius:12px;border:1px solid var(--border-color);">
        <div class="detail-card-header" style="flex-direction:column; align-items:flex-start; border-bottom:0; padding-bottom:8px;">
            <h2 style="font-size:15px;">Available Models</h2>
            <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">Add ${isCustom ? p.format : 'API'}-compatible models manually or import them from the /models endpoint.</div>
        </div>
        
        <div style="display:flex; gap:12px; margin-bottom:16px;">
            <div style="flex:1; border:1px solid var(--border-color); border-radius:8px; display:flex; align-items:center; padding:4px 4px 4px 12px; background:#fff;">
                <span style="color:var(--text-muted);font-size:12px;font-weight:600;margin-right:8px;">Model ID</span>
                <input type="text" id="inline-model-id" placeholder="e.g. claude-3-opus-20240229" style="flex:1; border:none; outline:none; font-size:13px; color:var(--text-main);" onkeydown="if(event.key==='Enter') saveManualModel()">
                <button class="btn" style="background:#f3f4f6; color:var(--text-muted); font-size:12px; font-weight:600; padding:6px 12px; border:none; border-radius:6px; display:flex; align-items:center; gap:4px;" onclick="saveManualModel()">
                    <span style="font-size:14px;line-height:1;">+</span> Add
                </button>
            </div>
            <button class="btn btn-outline" style="font-size:13px; font-weight:600; display:flex; align-items:center; gap:6px; border:none;" onclick="fetchModels(this)">
                <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                Import from /models
            </button>
        </div>

        <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; margin-bottom:12px;">
            <div style="display:flex; align-items:center; gap:8px; flex:1;">
                <div style="position:relative; flex:1; max-width:340px;">
                    <svg viewBox="0 0 24 24" width="14" height="14" stroke="var(--text-muted)" stroke-width="2" fill="none" style="position:absolute; left:8px; top:50%; transform:translateY(-50%);"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                    <input type="text" id="model-filter-input" placeholder="Filter models..." style="width:100%; padding:6px 10px 6px 28px; border:1px solid var(--border-color); border-radius:6px; font-size:12px; outline:none; background:var(--bg-body); color:var(--text-main);" oninput="applyModelFilter()">
                </div>
                <span id="model-filter-count" style="font-size:11px; color:var(--text-muted);"></span>
            </div>
            <div style="display:flex; gap:6px;">
                <button class="btn btn-outline" style="font-size:12px; padding:4px 12px; color:var(--success-color,#22c55e); border-color:var(--success-color,#22c55e);" onclick="enableFilteredModels()">Enable All</button>
                <button class="btn btn-outline" style="font-size:12px; padding:4px 12px;" onclick="disableFilteredModels()">Disable All</button>
                <button class="btn btn-danger" style="font-size:12px; padding:4px 12px; color:var(--danger); border-color:#fee2e2;" onclick="deleteAllModels()">Delete All</button>
            </div>
        </div>

        <div id="models-list-container" class="models-list" style="display:flex; flex-direction:column; gap:8px;">
            ${models.map((m, idx) => modelRow(m, idx, activeProviderId)).join('')}
            ${models.length === 0 ? '<div style="font-size:13px;color:var(--text-muted);padding:12px 0;">No models added yet.</div>' : ''}
        </div>
    </div>`;
}

function getThinkingSpec(modelId) {
    // Per-model reasoning capability spec — drives the precise Provider-tab
    // badges so each model only exposes the axes/values it actually supports.
    const id = (modelId || '').toLowerCase();

    // GPT-5.6 family (sol/terra/luna). Three reasoning axes, ALL verified via a
    // live probe against gpt-5.6-sol-pro20x:
    //   effort  — off/minimal/low/medium/high/xhigh/max (monotonic token scaling
    //             confirmed); 'max' is Sol-only.
    //   mode    — standard/pro ('pro' added ~+24% depth at max); Sol+Terra only.
    //   context — auto/current_turn/all_turns (cross-turn reasoning reuse).
    // 'ultra' is intentionally absent: the probe proved it is NOT a wire param
    // (client-side multi-agent orchestration only). Tier is matched by the
    // sol/terra/luna token so reseller variants like -pro20x resolve correctly.
    if (/gpt-?5\.6/.test(id)) {
        const context = ['auto','current_turn','all_turns'];
        if (/sol/.test(id))   return { effort: ['off','minimal','low','medium','high','xhigh','max'], mode: ['standard','pro'], context };
        if (/terra/.test(id)) return { effort: ['off','minimal','low','medium','high','xhigh'],       mode: ['standard','pro'], context };
        if (/luna/.test(id))  return { effort: ['off','minimal','low','medium','high','xhigh'],       mode: ['standard'],       context };
        return { effort: ['off','minimal','low','medium','high','xhigh'], context };  // preview / unknown tier
    }

    // xAI Grok 4.x: reasoning is mandatory (no 'off'), effort-only. Mirrors the
    // backend is_grok detector. The explicit *-non-reasoning SKU has no reasoning
    // engine, so it shows no controls.
    if (/grok/.test(id)) {
        if (/non-reasoning/.test(id)) return null;
        return { effort: ['low','medium','high'], mandatory: true };
    }

    // Anthropic Fable 5 / Mythos 5: effort + thinking mode + response display.
    if (/fable-?5|mythos-?5/.test(id)) {
        return { effort: ['off','low','medium','high','max'], mode: ['adaptive','enabled'], display: ['summarized','omitted'] };
    }

    // Kimi K3: a REAL reasoning_effort model (unlike K2). Checked BEFORE the
    // K2 branch so the broader `kimi|k2\.` pattern cannot swallow it.
    // Moonshot docs (2026-08-02): thinking is always-on; reasoning_effort
    // supports low/high/max (default max) — NO 'medium' (passing it 400s).
    // Backend contract: app/compat/families/kimi.py (id "kimi-k3").
    if (/kimi-?k3/.test(id)) return { effort: ['low','high','max'], mandatory: true };

    // Kimi K2 family. K2.7-code and any *-thinking SKU are always-on (the
    // parameter is a no-op). K2.5 / K2.6 are toggleable via enable_thinking.
    // K2 is BINARY — no graduated effort levels. K3 explicitly excluded above.
    if (/kimi|k2\./.test(id)) {
        if (/2\.7|thinking/.test(id)) return { alwaysOn: true };
        return { effort: ['off','enable'] };
    }

    // Antigravity Claude variants (anti*): budget-token tiers.
    if (/(claude|opus|sonnet).*anti|anti.*(claude|opus|sonnet)/.test(id)) return { effort: ['off','16k','32k'] };
    
    // All other Claude models (without anti*): adaptive mode + effort tiers.
    if (/(claude|opus|sonnet)/.test(id)) return { effort: ['off','low','medium','high','xhigh','max'], mode: ['adaptive'] };
    // DeepSeek V4.
    if (/deepseek-v4/.test(id)) return { effort: ['off','low','medium','high','max'] };
    // GLM-5.2.
    if (/glm-5\.2/.test(id)) return { effort: ['off','enable','low','high','max'] };
    // Qwen — TWO axes, both version-dependent. Checked BEFORE the generic
    // Chinese-model catch-all, which would otherwise offer every Qwen an
    // 'adaptive' that does not exist on the wire.
    // Official docs (https://docs.qwencloud.com/developer-guides/text-generation/thinking):
    //   qwen3.8-max      — enable_thinking + reasoning_effort {low, medium, xhigh};
    //                      default xhigh. There is NO 'high' and NO 'max'.
    //   qwen3.7 & older  — hybrid, enable_thinking bool ONLY; no effort enum
    //                      is published, so exposing tiers would be fiction.
    // Backend contract: app/compat/families/qwen.py (id "qwen").
    if (/qwen-?3\.8/.test(id)) return { effort: ['off','enable','low','medium','xhigh'] };
    if (/qwen/.test(id))       return { effort: ['off','enable'] };
    // Other Chinese reasoning models.
    if (/glm|mimo|minimax/.test(id)) return { effort: ['off','enable','adaptive'] };
    // Gemini.
    if (/gemini.*3/.test(id)) return { effort: ['off','low','medium','high'] };
    if (/gemini/.test(id)) return { effort: ['off','16k','32k'] };
    // GPT-5.4 / 5.5
    if (/gpt-?5\.[45]/.test(id)) return { effort: ['off','low','medium','high','xhigh'] };
    // Generic reasoning-capable fallback.
    if (/gpt-5|o1|o3|o4|openrouter/.test(id)) return { effort: ['off','low','medium','high','max'] };

    return null;  // no reasoning controls
}

function modelRow(m, idx, providerId) {
    const modelId = m.id || '';
    const thinking = m.thinking || 'auto';
    const routeKey = `${providerId}/${modelId}`;

    // Optional connection availability badge (multiple API keys under one provider).
    // Only render when manifest metadata is present so legacy rows are unchanged.
    let keyBadgeHtml = '';
    if (Array.isArray(m.connection_indexes) && m.connection_indexes.length > 0) {
        const provider = globalConfig.providers[providerId] || {};
        const connectionLabels = m.connection_indexes.map(connIdx => {
            const conn = (provider.connections || [])[connIdx] || {};
            const displayNo = Number.isInteger(connIdx) ? connIdx + 1 : connIdx;
            return `${conn.name || 'Connection ' + displayNo} (#${displayNo})`;
        });
        const count = m.connection_indexes.length;
        const label = count === 1 ? connectionLabels[0].replace(/ \(#\d+\)$/, '') : (count + ' keys');
        keyBadgeHtml = `<span title="Available on: ${connectionLabels.join(', ')}" style="font-size:11px;background:#ecfeff;color:#0e7490;padding:2px 6px;border-radius:4px;font-weight:600;">🔑 ${label}</span>`;
    }
    
    // Precise per-model reasoning badge. Always-on models (e.g. Kimi K2.7-code)
    // render a static badge instead of a no-op dropdown; multi-axis models
    // (GPT-5.6, Fable/Mythos) render one select per supported axis.
    const spec = getThinkingSpec(modelId);
    let thinkingOptionsHtml = '';
    if (spec && spec.alwaysOn) {
        thinkingOptionsHtml = `
            <div style="display:flex; align-items:center; gap:8px;">
                <span title="This model reasons on every request \u2014 no toggle needed" style="display:inline-flex;align-items:center;gap:5px;background:#ecfdf5;color:#047857;padding:3px 10px;border-radius:12px;font-weight:600;font-size:11px;border:1px solid #a7f3d0;">
                    <svg viewBox="0 0 24 24" width="12" height="12" stroke="currentColor" stroke-width="2.5" fill="none"><path d="M20 6L9 17l-5-5"/></svg>
                    Reasoning: Always-on
                </span>
            </div>`;
    } else if (spec) {
        const selStyle = 'padding:4px 24px 4px 8px; width:auto; border-radius:6px; font-size:11px; background-color:#f9fafb; cursor:pointer;';
        const buildSelect = (label, options, current, handler, allowAuto) => {
            const autoOpt = allowAuto ? `<option value="auto" ${current === 'auto' ? 'selected' : ''}>Auto</option>` : '';
            // Preserve a previously-saved value even if it is outside this model's
            // current option set (e.g. a legacy 'xhigh') so nothing is silently lost.
            const known = options.includes(current) || current === 'auto' || !current;
            const legacyOpt = known ? '' : `<option value="${current}" selected>${current} (current)</option>`;
            return `<label style="display:flex;align-items:center;gap:5px;font-size:11px;color:var(--text-muted);font-weight:600;">${label}
                <select class="input" style="${selStyle}" onchange="${handler}(${idx}, this.value)">
                    ${autoOpt}${legacyOpt}
                    ${options.map(o => `<option value="${o}" ${current === o ? 'selected' : ''}>${o}</option>`).join('')}
                </select></label>`;
        };
        let selects = '';
        // Order: Context → Mode → Effort → Display (Effort/Thinking sits nearest
        // the enable toggle, matching the approved badge layout).
        if (spec.context && spec.context.length) {
            const context = m.reasoning_context || spec.context[0];
            selects += buildSelect('Context', spec.context, context, 'updateModelContext', false);
        }
        if (spec.mode && spec.mode.length) {
            const mode = m.reasoning_mode || spec.mode[0];
            selects += buildSelect('Mode', spec.mode, mode, 'updateModelReasoningMode', false);
        }
        if (spec.effort && spec.effort.length) {
            const effort = m.thinking || (spec.mandatory ? spec.effort[spec.effort.length - 1] : 'auto');
            selects += buildSelect('Effort', spec.effort, effort, 'updateModelThinking', !spec.mandatory);
        }
        if (spec.display && spec.display.length) {
            const display = m.thinking_display || spec.display[0];
            selects += buildSelect('Display', spec.display, display, 'updateModelThinkingDisplay', false);
        }
        thinkingOptionsHtml = `<div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap; justify-content:flex-end;">${selects}</div>`;
    }

    return `
    <div class="model-row" data-model-id="${modelId}" style="display:flex; align-items:center; justify-content:space-between; padding:12px 16px; border:1px solid var(--border-color); border-radius:8px; background:var(--bg-surface);">
        <div style="display:flex; align-items:center; gap:16px;">
            <span style="color:var(--text-muted);">${SVGS.robot}</span>
            <div style="display:flex; flex-direction:column; gap:4px;">
                <div style="font-weight:600; font-size:13px; color:var(--text-main);">${modelId}</div>
                <div style="display:inline-block; font-size:11px; background:#f3f4f6; color:var(--text-muted); padding:2px 6px; border-radius:4px; width:fit-content;">${routeKey}</div>
                ${keyBadgeHtml}
            </div>
            <div style="display:flex; align-items:center; gap:8px; margin-left:8px;">
                <span class="model-action-btn" title="Copy" onclick="copyProviderModelId(${idx})">${SVGS.copy}</span>
                <span class="model-action-btn" title="Test" onclick="testProviderModel(${idx})" style="color:var(--text-muted);"><svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><path d="M10 2v7.31M14 9.31V2M8.5 2h7M14 9.31l6.4 9.6A2 2 0 0 1 18.73 22H5.27a2 2 0 0 1-1.66-3.09L10 9.31M6.5 16h11"/></svg></span>
            </div>
        </div>
        <div style="display:flex; align-items:center; gap:16px;">
            ${thinkingOptionsHtml}
            <div style="display:flex;align-items:center;gap:6px;border-left:1px solid var(--border-color);padding-left:12px;margin-left:4px;">
                <label class="switch switch-sm" title="Enable/Disable Model"><input type="checkbox" ${m.enabled !== false ? 'checked' : ''} onchange="toggleModelStatus(${idx}, this.checked)"><span class="slider" style="${m.enabled !== false ? 'background:var(--brand-color)' : ''}"></span></label>
            </div>
            <button class="btn btn-outline" style="padding:6px; border-color:#fee2e2; color:var(--danger); display:flex; align-items:center; justify-content:center; border-radius:6px;" onclick="removeModel(${idx})">
                <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
        </div>
    </div>`;
}

async function copyProviderModelId(idx) {
    const model = globalConfig.providers[activeProviderId]?.models?.[idx];
    const modelId = model?.id || '';
    if (!modelId) return;
    const routeKey = `${activeProviderId}/${modelId}`;
    try {
        await navigator.clipboard.writeText(routeKey);
        showToast(`Copied ${routeKey}`);
    } catch (err) {
        console.error('Copy model failed', err);
        showToast('Failed to copy model ID', true);
    }
}

async function testProviderModel(idx) {
    const provider = globalConfig.providers[activeProviderId];
    const model = provider?.models?.[idx];
    const modelId = model?.id || '';
    if (!activeProviderId || !modelId) {
        showToast('No provider/model selected', true);
        return;
    }
    const routedModel = `${activeProviderId}/${modelId}`;
    showToast(`Testing ${routedModel}...`);
    try {
        const resp = await fetch('/api/test-model', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider: activeProviderId, model: modelId })
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.ok === false) {
            const msg = data.error || data.detail || `HTTP ${resp.status}`;
            showToast(`Test failed: ${msg}`, true);
            return;
        }
        showToast(`Test OK: ${routedModel}`);
    } catch (err) {
        console.error('Model test failed', err);
        showToast(`Test failed: ${err.message}`, true);
    }
}

window.copyProviderModelId = copyProviderModelId;
window.testProviderModel = testProviderModel;

window.saveManualModel = () => {
    const el = document.getElementById('inline-model-id');
    const id = el.value.trim();
    if (!id) {
        alert('Model ID is required.');
        return;
    }
    if (!globalConfig.providers[activeProviderId].models) globalConfig.providers[activeProviderId].models = [];
    const exists = globalConfig.providers[activeProviderId].models.some(m => m.id === id);
    if (exists) {
        alert(`Model "${id}" already exists.`);
        return;
    }
    globalConfig.providers[activeProviderId].models.push({ id, name: id, thinking: 'auto' });
    el.value = '';
    saveConfig();
    renderActiveTab();
};

window.deleteAllModels = () => {
    const q = (document.getElementById('model-filter-input')?.value || '').toLowerCase().trim();
    const models = globalConfig.providers[activeProviderId]?.models || [];
    if (models.length === 0) return;
    let toDelete, msg;
    if (q) {
        toDelete = models.filter(m => (m.id || '').toLowerCase().includes(q));
        if (toDelete.length === 0) return;
        msg = `Delete ${toDelete.length} filtered model(s) matching "${q}" from this provider?`;
    } else {
        toDelete = models;
        msg = `Delete all ${models.length} models from this provider?`;
    }
    if (!confirm(msg)) return;
    const idsToDelete = new Set(toDelete.map(m => m.id));
    globalConfig.providers[activeProviderId].models = models.filter(m => !idsToDelete.has(m.id));
    saveConfig();
    renderActiveTab();
    if (q) {
        setTimeout(() => {
            const filterEl = document.getElementById('model-filter-input');
            if (filterEl) { filterEl.value = q; applyModelFilter(); }
        }, 50);
    }
};

// Save thinking config silently — no toast notification, no auto-test.
// The user will explicitly test via the Test button if needed.
window.updateModelThinking = (idx, value) => {
    if (globalConfig.providers[activeProviderId].models[idx]) {
        globalConfig.providers[activeProviderId].models[idx].thinking = value;
        // Silent save: persist to backend without showing UI notifications
        fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(globalConfig)
        }).catch(err => console.error('Silent thinking save failed:', err));
    }
};

// Save reasoning mode (GPT-5.6 standard/pro/ultra, Fable/Mythos adaptive/enabled).
window.updateModelReasoningMode = (idx, value) => {
    const models = globalConfig.providers[activeProviderId]?.models;
    if (models && models[idx]) {
        models[idx].reasoning_mode = value;
        fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(globalConfig)
        }).catch(err => console.error('Silent reasoning-mode save failed:', err));
    }
};

// Save reasoning context (GPT-5.6 auto/current_turn/all_turns — controls
// cross-turn reasoning reuse). Verified as a live reasoning.context wire param.
window.updateModelContext = (idx, value) => {
    const models = globalConfig.providers[activeProviderId]?.models;
    if (models && models[idx]) {
        models[idx].reasoning_context = value;
        fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(globalConfig)
        }).catch(err => console.error('Silent reasoning-context save failed:', err));
    }
};

// Save thinking display preference (Fable/Mythos summarized/omitted).
window.updateModelThinkingDisplay = (idx, value) => {
    const models = globalConfig.providers[activeProviderId]?.models;
    if (models && models[idx]) {
        models[idx].thinking_display = value;
        fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(globalConfig)
        }).catch(err => console.error('Silent thinking-display save failed:', err));
    }
};

window.toggleModelStatus = (idx, isEnabled) => {
    const p = globalConfig.providers[activeProviderId];
    if (p && p.models && p.models[idx] !== undefined) {
        p.models[idx].enabled = isEnabled;
        saveConfig();
        // Re-render only the slider's background without full re-render to avoid focus loss
        const sliders = document.querySelectorAll('.models-list .switch-sm .slider');
        if (sliders[idx]) {
            sliders[idx].style.background = isEnabled ? 'var(--brand-color)' : '';
        }
    }
};

// ... keep existing openModelModal for backwards compatibility or remove if not used.
window.openModelModal = () => { };
window.closeModelModal = () => { };
window.saveModelModal = () => { };

window.disableAllModels = () => {
    const models = globalConfig.providers[activeProviderId].models || [];
    if (models.length === 0) return;
    models.forEach(m => m.enabled = false);
    saveConfig();
    renderActiveTab();
};

window.applyModelFilter = () => {
    const q = (document.getElementById('model-filter-input')?.value || '').toLowerCase();
    const container = document.getElementById('models-list-container');
    if (!container) return;
    const rows = container.querySelectorAll('.model-row');
    let visible = 0;
    rows.forEach(row => {
        const id = (row.dataset.modelId || '').toLowerCase();
        const show = !q || id.includes(q);
        row.style.display = show ? 'flex' : 'none';
        if (show) visible++;
    });
    const countEl = document.getElementById('model-filter-count');
    if (countEl) {
        const total = rows.length;
        countEl.textContent = q ? `${visible} / ${total} models` : `${total} models`;
    }
};

window.enableFilteredModels = () => {
    const q = (document.getElementById('model-filter-input')?.value || '').toLowerCase();
    const models = globalConfig.providers[activeProviderId]?.models || [];
    models.forEach(m => {
        if (!q || m.id.toLowerCase().includes(q)) m.enabled = true;
    });
    saveConfig();
    renderActiveTab();
    // Re-apply the filter after re-render
    setTimeout(() => {
        const filterEl = document.getElementById('model-filter-input');
        if (filterEl && q) { filterEl.value = q; applyModelFilter(); }
    }, 50);
};

window.disableFilteredModels = () => {
    const q = (document.getElementById('model-filter-input')?.value || '').toLowerCase();
    const models = globalConfig.providers[activeProviderId]?.models || [];
    models.forEach(m => {
        if (!q || m.id.toLowerCase().includes(q)) m.enabled = false;
    });
    saveConfig();
    renderActiveTab();
    setTimeout(() => {
        const filterEl = document.getElementById('model-filter-input');
        if (filterEl && q) { filterEl.value = q; applyModelFilter(); }
    }, 50);
};

const CUSTOM_TEXT_FORMATS = new Set(['openai', 'openai-responses', 'anthropic', 'gemini']);
const CUSTOM_TEXT_SUFFIXES = [
    '/v1/chat/completions', '/chat/completions', '/v1/messages',
    '/messages', '/v1/responses', '/responses'
];

function isCustomTextFormat(format) {
    return CUSTOM_TEXT_FORMATS.has(String(format || '').toLowerCase());
}

function customTextAppendedPath(format) {
    return String(format || '').toLowerCase() === 'anthropic'
        ? '/v1/messages'
        : '/v1/chat/completions';
}

function normalizeCustomTextBaseUrl(value) {
    const raw = String(value || '').trim().replace(/\/+$/, '');
    let parsed;
    try {
        parsed = new URL(raw);
    } catch {
        return { value: raw, corrected: false, error: 'Base URL must use http:// or https:// and include a host.' };
    }
    if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password) {
        return { value: raw, corrected: false, error: 'Base URL must use http:// or https:// and include a host.' };
    }
    if (parsed.search || parsed.hash) {
        return { value: raw, corrected: false, error: 'Base URL must not include a query string or fragment.' };
    }

    let path = parsed.pathname.replace(/\/+$/, '');
    const originalPath = path;
    const lowered = path.toLowerCase();
    for (const suffix of CUSTOM_TEXT_SUFFIXES) {
        if (lowered.endsWith(suffix)) {
            path = path.slice(0, -suffix.length).replace(/\/+$/, '');
            break;
        }
    }
    if (path.toLowerCase().endsWith('/v1')) {
        path = path.slice(0, -3).replace(/\/+$/, '');
    }
    parsed.pathname = path || '/';
    const normalized = parsed.toString().replace(/\/$/, '');
    return { value: normalized, corrected: normalized !== raw || path !== originalPath, error: '' };
}

function setUrlFieldNotice(fieldId, noticeId, result) {
    const field = document.getElementById(fieldId);
    const notice = document.getElementById(noticeId);
    if (!field || !notice) return;
    field.classList.toggle('input-error', Boolean(result.error));
    notice.className = 'url-field-notice';
    notice.textContent = '';
    if (result.error) {
        notice.classList.add('error');
        notice.textContent = result.error;
    } else if (result.corrected) {
        notice.classList.add('warning');
        notice.textContent = 'BSL removed the terminal API suffix. Enter the provider URL only; BSL adds the API path automatically.';
    }
}

function normalizeUrlField(fieldId, noticeId, format, allowBlank = false) {
    const field = document.getElementById(fieldId);
    if (!field || !isCustomTextFormat(format)) return { value: field ? field.value.trim() : '', corrected: false, error: '' };
    if (!field.value.trim() && allowBlank) {
        setUrlFieldNotice(fieldId, noticeId, { corrected: false, error: '' });
        return { value: '', corrected: false, error: '' };
    }
    const result = normalizeCustomTextBaseUrl(field.value);
    if (!result.error) field.value = result.value;
    setUrlFieldNotice(fieldId, noticeId, result);
    return result;
}

function updateCustomUrlHelp() {
    const providerFormat = document.getElementById('p-format')?.value || 'openai';
    const providerHelp = document.getElementById('p-url-path-help');
    if (providerHelp) providerHelp.textContent = isCustomTextFormat(providerFormat)
        ? `BSL appends ${customTextAppendedPath(providerFormat)} automatically.` : '';
    const connFormat = globalConfig.providers?.[activeProviderId]?.format || 'openai';
    const connHelp = document.getElementById('conn-url-path-help');
    if (connHelp) connHelp.textContent = isCustomTextFormat(connFormat)
        ? `BSL appends ${customTextAppendedPath(connFormat)} automatically.` : '';
}

// Resolve a connection's effective base URL using the inheritance chain
// (current edit value / connection base_url / known provider URL /
//  provider-level base_url). Returns "" if nothing is found.
// `editConnIdx` (-1 for none) lets the connection modal pre-fill inherit the
// value currently typed into the modal URL field.
function resolveBaseUrl(providerId, connIdx, editConnIdx = -1) {
    const p = globalConfig.providers[providerId] || {};
    const knownProvider = KNOWN_PROVIDERS.api.find(kp => kp.id === providerId)
                       || KNOWN_PROVIDERS.oauth.find(kp => kp.id === providerId);

    // 1. current edit value (if a connection modal is open and field is visible)
    if (editConnIdx >= 0) {
        const editEl = document.getElementById('conn-url');
        if (editEl && editEl.offsetParent !== null) {
            const v = (editEl.value || '').trim();
            if (v) return v;
        }
    }
    // 2. this connection's stored base_url
    const conn = (p.connections || [])[connIdx] || {};
    if (conn.base_url) return conn.base_url;
    // 3. known provider URL
    if (knownProvider && knownProvider.url) return knownProvider.url;
    // 4. provider-level base_url (legacy/custom)
    if (p.base_url) return p.base_url;
    // 5. first existing connection base_url (inherit from siblings)
    const sib = (p.connections || []).find(c => c && c.base_url);
    if (sib) return sib.base_url;
    return '';
}

window.fetchModels = async (btn) => {
    const p = globalConfig.providers[activeProviderId];
    if (!p || !p.connections || p.connections.length === 0) {
        alert('Please add and configure a connection first.');
        return;
    }

    // Iterate ALL enabled connections with API keys (not just index 0).
    const enabledConns = p.connections
        .map((c, i) => ({ c, i }))
        .filter(({ c }) => (c.enabled !== false) && (c.api_key || '').trim());

    if (enabledConns.length === 0) {
        alert('No enabled connections with an API key found. Add at least one key, then retry.');
        return;
    }

    if (!globalConfig.providers[activeProviderId].models) {
        globalConfig.providers[activeProviderId].models = [];
    }
    const models = globalConfig.providers[activeProviderId].models;

    const origText = btn.innerHTML;
    btn.innerHTML = 'Fetching...';
    btn.disabled = true;

    let added = 0, availabilityUpdates = 0, failed = 0;
    const failures = [];

    await Promise.all(enabledConns.map(async ({ c, i }) => {
        const baseUrl = resolveBaseUrl(activeProviderId, i);
        const key = (c.api_key || '').trim();
        if (!baseUrl || !key) {
            failed++;
            failures.push(`#${i + 1} ${c.name || ''}: missing base URL or key`.trim());
            return;
        }
        try {
            const res = await fetch('/api/verify-key', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ provider_id: activeProviderId, format: p.format, api_key: key, base_url: baseUrl })
            });
            const respData = await res.json();
            if (respData.ok && respData.data && Array.isArray(respData.data.data)) {
                const fetchedIds = respData.data.data.map(m => m.id);
                fetchedIds.forEach(id => {
                    let entry = models.find(m => m.id === id);
                    if (!entry) {
                        entry = { id, name: id, thinking: 'auto', connection_indexes: [i] };
                        models.push(entry);
                        added++;
                    }
                    // Update connection availability metadata — union of returning indexes.
                    if (!Array.isArray(entry.connection_indexes)) {
                        entry.connection_indexes = [];
                    }
                    if (!entry.connection_indexes.includes(i)) {
                        entry.connection_indexes.push(i);
                        entry.connection_indexes.sort((a, b) => a - b);
                        availabilityUpdates++;
                    }
                });
            } else {
                failed++;
                const msg = (respData.error || ('status ' + (respData.status || 'unknown'))).toString();
                failures.push(`#${i + 1} ${c.name || ''}: ${msg}`.trim());
            }
        } catch (err) {
            failed++;
            failures.push(`#${i + 1} ${c.name || ''}: ${err.message}`.trim());
        }
    }));

    try {
        saveConfig();
    } catch (e) { /* saveConfig best-effort */ }
    renderActiveTab();

    const t = document.getElementById('toast');
    const summary = `Checked ${enabledConns.length} connection${enabledConns.length > 1 ? 's' : ''} · ${added} added · ${availabilityUpdates} availability updates · ${failed} failed.`;
    if (failed === enabledConns.length) {
        // All connections failed — surface concise per-connection details.
        alert(`All ${failed} connection${failed > 1 ? 's' : ''} failed to return models.\n\n${failures.join('\n')}\n\nNote: Some providers do not support the /models endpoint. Type the exact model ID from this provider into the box on the left and click "+ Add" manually.`);
    } else if (t) {
        t.textContent = summary;
        t.classList.add('show');
        setTimeout(() => { t.classList.remove('show'); t.textContent = 'Settings saved successfully'; }, 3500);
    } else {
        console.log(summary);
    }

    btn.innerHTML = origText;
    btn.disabled = false;
};

window.verifyProviderKey = async (btn) => {
    const format = document.getElementById('p-format').value;
    const normalized = normalizeUrlField('p-url', 'p-url-notice', format, false);
    const key = document.getElementById('p-key').value.trim();
    if (normalized.error || !normalized.value || !key) {
        if (!key) alert('Please enter an API key first.');
        return;
    }
    const origText = btn.textContent;
    btn.textContent = 'Checking...';
    btn.disabled = true;
    try {
        const providerId = editingProviderId || document.getElementById('p-prefix').value.trim();
        const res = await fetch('/api/verify-key', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider_id: providerId, format, api_key: key, base_url: normalized.value })
        });
        const data = await res.json();
        btn.textContent = data.ok ? '✓ Valid' : '✗ Invalid';
        btn.style.color = data.ok ? 'var(--success)' : 'var(--danger)';
    } catch {
        btn.textContent = '? Error';
        btn.style.color = 'var(--text-muted)';
    } finally {
        btn.disabled = false;
        setTimeout(() => { btn.textContent = origText; btn.style.color = ''; }, 3000);
    }
};

window.removeModel = (idx) => {
    globalConfig.providers[activeProviderId].models.splice(idx, 1);
    saveConfig();
    renderActiveTab();
};

window.deleteActiveProvider = () => {
    if (confirm('Delete this custom provider?')) {
        delete globalConfig.providers[activeProviderId];
        backToList();
    }
};

window.deleteConnection = async (idx) => {
    if (confirm('Delete this connection?')) {
        globalConfig.providers[activeProviderId].connections.splice(idx, 1);
        await saveConfig();
        renderActiveTab();
    }
};

window.toggleConnection = async (idx, enabled) => {
    globalConfig.providers[activeProviderId].connections[idx].enabled = enabled;
    await saveConfig();
    renderActiveTab();
};

window.toggleProviderRoundRobin = async (enabled) => {
    if (!globalConfig.providers[activeProviderId]) return;
    globalConfig.providers[activeProviderId].round_robin = enabled;
    await saveConfig();
    renderActiveTab();
};

window.moveConnectionUp = async (idx) => {
    if (idx <= 0) return;
    const conns = globalConfig.providers[activeProviderId].connections;
    const temp = conns[idx];
    conns[idx] = conns[idx - 1];
    conns[idx - 1] = temp;
    await saveConfig();
    renderActiveTab();
};

window.moveConnectionDown = async (idx) => {
    const conns = globalConfig.providers[activeProviderId].connections;
    if (idx >= conns.length - 1) return;
    const temp = conns[idx];
    conns[idx] = conns[idx + 1];
    conns[idx + 1] = temp;
    await saveConfig();
    renderActiveTab();
};

function escapeOAuthHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[char]);
}

function oauthErrorMessage(data, fallback) {
    if (!data || typeof data !== 'object') return fallback;
    return data.detail || data.errorDescription || data.error_description || data.error || data.message || fallback;
}

async function oauthResponseData(response, fallback) {
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(oauthErrorMessage(data, `${fallback} (HTTP ${response.status})`));
    return data;
}

function showOAuthModal({ title, body, onClose }) {
    const existing = document.getElementById('oauth-modal-overlay');
    if (existing?._oauthClose) existing._oauthClose();
    else if (existing) existing.remove();

    if (!document.getElementById('oauth-modal-styles')) {
        const style = document.createElement('style');
        style.id = 'oauth-modal-styles';
        style.textContent = '@keyframes oauth-spin { to { transform: rotate(360deg); } }';
        document.head.appendChild(style);
    }

    const overlay = document.createElement('div');
    overlay.id = 'oauth-modal-overlay';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:10000;display:flex;align-items:center;justify-content:center;padding:16px;background:rgba(0,0,0,0.4);';

    const panel = document.createElement('section');
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'true');
    panel.style.cssText = 'width:min(520px,100%);max-height:calc(100vh - 32px);overflow:auto;background:var(--bg-surface);border:1px solid var(--border-color);border-radius:12px;box-shadow:0 10px 25px rgba(0,0,0,0.1);color:var(--text-main);animation:modalIn 0.2s ease-out;';

    const header = document.createElement('div');
    header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:16px;padding:18px 20px;border-bottom:1px solid var(--border-color);';
    const heading = document.createElement('h3');
    heading.style.cssText = 'margin:0;font-size:17px;font-weight:650;';
    heading.textContent = title;
    const closeButton = document.createElement('button');
    closeButton.type = 'button';
    closeButton.setAttribute('aria-label', 'Close OAuth dialog');
    closeButton.textContent = '×';
    closeButton.style.cssText = 'border:0;background:transparent;color:var(--text-muted);font-size:26px;line-height:1;cursor:pointer;padding:0 2px;';
    header.append(heading, closeButton);

    const content = document.createElement('div');
    content.style.cssText = 'padding:20px;';
    content.innerHTML = body;
    panel.append(header, content);
    overlay.appendChild(panel);
    document.body.appendChild(overlay);

    let closed = false;
    const close = () => {
        if (closed) return;
        closed = true;
        overlay.remove();
        if (typeof onClose === 'function') onClose();
    };
    overlay._oauthClose = close;
    closeButton.addEventListener('click', close);
    overlay.addEventListener('click', event => {
        if (event.target === overlay) close();
    });

    return { overlay, content, close };
}

function setOAuthModalMessage(element, message, isError = false) {
    if (!element) return;
    element.textContent = message;
    element.style.display = 'block';
    element.style.color = isError ? 'var(--danger)' : 'var(--text-muted)';
}

async function completeOAuthConnection(providerId, connection, modal) {
    modal.close();
    await fetchConfig(); // OAuth endpoints persist directly; rehydrate the authoritative config snapshot.
    const account = connection?.displayName || connection?.email || getDisplayName(providerId);
    showToast(`Connected ${account}`);
}

async function startFixedPortLoopbackFlow(providerId) {
    const name = getDisplayName(providerId);
    let pollTimer = null;
    let cancelled = false;
    const modal = showOAuthModal({
        title: `Connect ${name}`,
        body: `<div style="text-align:center;padding:24px;"><div style="width:24px;height:24px;margin:0 auto 14px;border:2px solid var(--border-color);border-top-color:var(--brand-color);border-radius:50%;animation:oauth-spin .8s linear infinite;"></div><div style="font-size:14px;color:var(--text-muted);">Preparing a secure sign-in…</div></div>`,
        onClose: () => {
            cancelled = true;
            if (pollTimer) clearTimeout(pollTimer);
            fetch(`/api/oauth/${encodeURIComponent(providerId)}/stop-proxy`, { method: 'POST' }).catch(() => {});
        }
    });

    try {
        const authorization = await oauthResponseData(
            await fetch(`/api/oauth/${encodeURIComponent(providerId)}/authorize`),
            `Could not start ${name} OAuth authorization`
        );
        const isPkce = authorization.flowType === 'authorization_code_pkce';
        if (!authorization.authUrl || !authorization.state || !authorization.redirectUri || (isPkce && !authorization.codeVerifier)) {
            throw new Error(`${name} OAuth authorization response was incomplete.`);
        }
        if (!modal.overlay.isConnected) return;

        const state = authorization.state;
        const appPort = window.location.port || '6969';
        const codeVerifier = authorization.codeVerifier || '';
        const proxyResult = await oauthResponseData(
            await fetch(`/api/oauth/${encodeURIComponent(providerId)}/start-proxy?app_port=${encodeURIComponent(appPort)}&state=${encodeURIComponent(state)}&code_verifier=${encodeURIComponent(codeVerifier)}&redirect_uri=${encodeURIComponent(authorization.redirectUri)}`),
            `Could not start ${name} callback listener`
        );

        if (proxyResult.serverSide) {
            modal.content.innerHTML = `
                <div style="display:flex;flex-direction:column;gap:16px;">
                    <p style="margin:0;font-size:14px;line-height:1.5;color:var(--text-muted);">Open the provider sign-in page and approve access. This dialog updates automatically after the loopback callback completes.</p>
                    <a id="oauth-loopback-link" class="btn btn-primary" target="_blank" rel="noopener noreferrer" style="display:flex;justify-content:center;align-items:center;text-decoration:none;">Open ${escapeOAuthHtml(name)} sign-in</a>
                    <div id="oauth-loopback-status" style="text-align:center;font-size:12px;line-height:1.4;color:var(--text-muted);">Waiting for approval…</div>
                </div>`;
            modal.content.querySelector('#oauth-loopback-link').href = authorization.authUrl;
            const statusEl = modal.content.querySelector('#oauth-loopback-status');
            const poll = async () => {
                if (cancelled || !modal.overlay.isConnected) return;
                try {
                    const result = await oauthResponseData(
                        await fetch(`/api/oauth/${encodeURIComponent(providerId)}/poll-status?state=${encodeURIComponent(state)}`),
                        `${name} status poll failed`
                    );
                    if (result.status === 'done' && result.connection) {
                        await completeOAuthConnection(providerId, result.connection, modal);
                        return;
                    }
                    if (result.status === 'error' || result.status === 'unknown') {
                        setOAuthModalMessage(statusEl, oauthErrorMessage(result, result.status === 'unknown' ? 'Session expired. Close and try again.' : `${name} OAuth failed.`), true);
                        return;
                    }
                } catch (error) {
                    setOAuthModalMessage(statusEl, error.message || `${name} status poll failed.`, true);
                    return;
                }
                if (!cancelled && modal.overlay.isConnected) pollTimer = setTimeout(poll, 2000);
            };
            pollTimer = setTimeout(poll, 2000);
            return;
        }

        const fallbackMsg = proxyResult.reason || 'Callback listener unavailable. Paste the callback URL or authorization code below.';
        modal.content.innerHTML = `
            <div style="display:flex;flex-direction:column;gap:16px;">
                <p style="margin:0;font-size:14px;line-height:1.5;color:var(--text-muted);">${escapeOAuthHtml(fallbackMsg)}</p>
                <a id="oauth-loopback-link" class="btn btn-primary" target="_blank" rel="noopener noreferrer" style="display:flex;justify-content:center;align-items:center;text-decoration:none;">Open ${escapeOAuthHtml(name)} sign-in</a>
                <form id="oauth-loopback-form" style="display:flex;flex-direction:column;gap:10px;">
                    <label for="oauth-loopback-callback" style="font-size:12px;font-weight:600;color:var(--text-main);">Callback URL or authorization code</label>
                    <input id="oauth-loopback-callback" class="input" type="text" required autocomplete="off" placeholder="Paste the callback URL or authorization code" style="width:100%;box-sizing:border-box;">
                    <div id="oauth-loopback-message" style="display:none;font-size:12px;line-height:1.4;"></div>
                    <button id="oauth-loopback-submit" class="btn btn-primary" type="submit" style="align-self:flex-end;">Finish connection</button>
                </form>
            </div>`;
        modal.content.querySelector('#oauth-loopback-link').href = authorization.authUrl;
        const form = modal.content.querySelector('#oauth-loopback-form');
        const callbackInput = modal.content.querySelector('#oauth-loopback-callback');
        const submitBtn = modal.content.querySelector('#oauth-loopback-submit');
        const message = modal.content.querySelector('#oauth-loopback-message');
        callbackInput.focus();
        form.addEventListener('submit', async event => {
            event.preventDefault();
            const pasted = callbackInput.value.trim();
            let code = pasted;
            try {
                const callback = new URL(pasted);
                const providerError = callback.searchParams.get('error');
                if (providerError) throw new Error(callback.searchParams.get('error_description') || providerError);
                code = callback.searchParams.get('code') || callback.searchParams.get('id_token') || pasted;
            } catch (error) {
                if (error.message && error.message !== 'Invalid URL') {
                    setOAuthModalMessage(message, error.message, true);
                    return;
                }
            }
            if (!code) {
                setOAuthModalMessage(message, 'Paste the callback URL or authorization code.', true);
                return;
            }
            submitBtn.disabled = true;
            submitBtn.textContent = 'Connecting…';
            try {
                // Codex simplified flow can return a JWT id_token; 9router posts it to /exchange as an access token.
                const result = await oauthResponseData(
                    await fetch(`/api/oauth/${encodeURIComponent(providerId)}/exchange`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ code, state, codeVerifier: authorization.codeVerifier, redirectUri: authorization.redirectUri }),
                    }),
                    `${name} token exchange failed`
                );
                if (!result.success || !result.connection) throw new Error(oauthErrorMessage(result, `${name} OAuth was not completed.`));
                await completeOAuthConnection(providerId, result.connection, modal);
            } catch (error) {
                if (modal.overlay.isConnected) {
                    setOAuthModalMessage(message, error.message || `${name} OAuth failed.`, true);
                    submitBtn.disabled = false;
                    submitBtn.textContent = 'Finish connection';
                }
            }
        });
    } catch (error) {
        if (modal.overlay.isConnected) {
            modal.content.innerHTML = `<div style="text-align:center;padding:18px 8px;color:var(--danger);font-size:14px;line-height:1.5;"></div>`;
            modal.content.firstElementChild.textContent = error.message || `Could not start ${name} OAuth.`;
        }
    }
}

async function startCodexLoopback() {
    return startFixedPortLoopbackFlow('codex');
}

async function startNativeTokenImport(providerId) {
    const name = getDisplayName(providerId);
    const modal = showOAuthModal({
        title: `Import ${name}`,
        body: `<div style="text-align:center;padding:24px;font-size:14px;color:var(--text-muted);">Reading the native ${escapeOAuthHtml(name)} token store…</div>`
    });
    try {
        const result = await oauthResponseData(
            await fetch(`/api/oauth/${encodeURIComponent(providerId)}/import`, { method: 'POST' }),
            `Could not import ${name} token`
        );
        if (!result.success || !result.connection) throw new Error(oauthErrorMessage(result, `${name} token import failed.`));
        await completeOAuthConnection(providerId, result.connection, modal);
    } catch (error) {
        if (modal.overlay.isConnected) {
            modal.content.innerHTML = `<div style="text-align:center;padding:18px 8px;color:var(--danger);font-size:14px;line-height:1.5;"></div>`;
            modal.content.firstElementChild.textContent = error.message || `Could not import ${name} token.`;
        }
    }
}

async function startAuthCodeFlow(providerId) {
    const providerConfig = OAUTH_PROVIDER_CONFIG[providerId] || {};
    if (providerConfig.fixedPort) {
        if (providerId === 'openai_codex') return startCodexLoopback();
        return startFixedPortLoopbackFlow(providerId);
    }
    const name = getDisplayName(providerId);
    let pollTimer = null;
    let cancelled = false;
    let popup = null;
    const modal = showOAuthModal({
        title: `Connect ${name}`,
        body: `<div style="text-align:center;padding:24px;"><div style="width:24px;height:24px;margin:0 auto 14px;border:2px solid var(--border-color);border-top-color:var(--brand-color);border-radius:50%;animation:oauth-spin .8s linear infinite;"></div><div style="font-size:14px;color:var(--text-muted);">Preparing a secure sign-in…</div></div>`,
        onClose: () => {
            cancelled = true;
            if (pollTimer) clearTimeout(pollTimer);
            if (popup) popup.close();
        }
    });

    try {
        const authorization = await oauthResponseData(
            await fetch(`/api/oauth/${encodeURIComponent(providerId)}/authorize`),
            `Could not start ${name} OAuth authorization`
        );
        if (!authorization.authUrl || !authorization.state || !authorization.redirectUri) {
            throw new Error(`${name} OAuth authorization response was incomplete.`);
        }
        if (!modal.overlay.isConnected) return;

        const state = authorization.state;
        popup = window.open(authorization.authUrl, '_blank', 'width=600,height=800');

        modal.content.innerHTML = `
            <div style="display:flex;flex-direction:column;gap:16px;text-align:center;padding:8px;">
                <p style="margin:0;font-size:14px;line-height:1.5;color:var(--text-muted);">Open the sign-in page to authorize BSL Router. The connection will complete automatically.</p>
                <a id="oauth-open-btn" class="btn btn-primary" href="${escapeOAuthHtml(authorization.authUrl)}" target="_blank" rel="noopener noreferrer" style="text-decoration:none;">Open sign-in page</a>
                <div id="oauth-status-text" style="font-size:13px;color:var(--brand-color);display:flex;align-items:center;justify-content:center;gap:8px;margin-top:8px;">
                    <div style="width:14px;height:14px;border:2px solid var(--border-color);border-top-color:var(--brand-color);border-radius:50%;animation:oauth-spin .8s linear infinite;"></div>
                    Waiting for authorization…
                </div>
            </div>`;

        const statusText = modal.content.querySelector('#oauth-status-text');
        
        const poll = async () => {
            if (cancelled || !modal.overlay.isConnected) return;
            try {
                const result = await oauthResponseData(
                    await fetch(`/api/oauth/${encodeURIComponent(providerId)}/poll-status?state=${encodeURIComponent(state)}`),
                    `${name} status poll failed`
                );
                if (result.status === 'done' && result.connection) {
                    if (popup) popup.close();
                    await completeOAuthConnection(providerId, result.connection, modal);
                    return;
                }
                if (result.status === 'error') {
                    if (popup) popup.close();
                    setOAuthModalMessage(statusText, oauthErrorMessage(result, `${name} OAuth failed.`), true);
                    return;
                }
            } catch (error) {
                setOAuthModalMessage(statusText, error.message || `${name} status poll failed.`, true);
                return;
            }
            if (!cancelled && modal.overlay.isConnected) pollTimer = setTimeout(poll, 2000);
        };
        pollTimer = setTimeout(poll, 2000);
    } catch (error) {
        if (modal.overlay.isConnected) {
            modal.content.innerHTML = `<div style="text-align:center;padding:18px 8px;color:var(--danger);font-size:14px;line-height:1.5;"></div>`;
            modal.content.firstElementChild.textContent = error.message || 'Could not start OAuth authorization.';
        }
    }
}

async function startDeviceCodeFlow(providerId, query = null) {
    const name = getDisplayName(providerId);
    let pollTimer = null;
    let cancelled = false;
    const modal = showOAuthModal({
        title: `Connect ${name}`,
        body: `<div style="text-align:center;padding:24px;"><div style="width:24px;height:24px;margin:0 auto 14px;border:2px solid var(--border-color);border-top-color:var(--brand-color);border-radius:50%;animation:oauth-spin .8s linear infinite;"></div><div style="font-size:14px;color:var(--text-muted);">Requesting a device code…</div></div>`,
        onClose: () => {
            cancelled = true;
            if (pollTimer) clearTimeout(pollTimer);
        }
    });

    try {
        const device = await oauthResponseData(
            await fetch(`/api/oauth/${encodeURIComponent(providerId)}/device-code${query ? `?${query}` : ''}`),
            'Could not start device authorization'
        );
        const verificationUrl = device.verification_uri_complete || device.verification_uri;
        if (!device.device_code || !verificationUrl) throw new Error('Device authorization response was incomplete.');
        if (!modal.overlay.isConnected) return;

        const userCode = device.user_code || 'No code required';
        modal.content.innerHTML = `
            <div style="display:flex;flex-direction:column;align-items:center;gap:16px;text-align:center;">
                <p style="margin:0;font-size:14px;line-height:1.5;color:var(--text-muted);">Open the verification page and enter this code to approve ${escapeOAuthHtml(name)}.</p>
                <div style="padding:12px 20px;border:1px solid var(--border-color);border-radius:10px;background:rgba(255,255,255,.04);font-family:monospace;font-size:24px;font-weight:700;letter-spacing:3px;">${escapeOAuthHtml(userCode)}</div>
                <div style="display:flex;gap:10px;flex-wrap:wrap;justify-content:center;">
                    <a id="oauth-device-link" class="btn btn-primary" target="_blank" rel="noopener noreferrer" style="text-decoration:none;">Open verification page</a>
                    <button id="oauth-copy-device-code" class="btn btn-outline" type="button">Copy code</button>
                </div>
                <div id="oauth-device-message" style="font-size:12px;line-height:1.4;color:var(--text-muted);">Waiting for approval…</div>
            </div>`;

        modal.content.querySelector('#oauth-device-link').href = verificationUrl;
        const copyButton = modal.content.querySelector('#oauth-copy-device-code');
        const message = modal.content.querySelector('#oauth-device-message');
        copyButton.addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(userCode);
                copyButton.textContent = 'Copied';
                setTimeout(() => { if (modal.overlay.isConnected) copyButton.textContent = 'Copy code'; }, 1500);
            } catch {
                setOAuthModalMessage(message, 'Copy is unavailable in this browser. Select the code manually.', true);
            }
        });

        const interval = Math.max(1, Number(device.interval) || 5) * 1000;
        const expiresAt = Date.now() + Math.max(1, Number(device.expires_in) || 900) * 1000;
        const poll = async () => {
            if (cancelled || !modal.overlay.isConnected) return;
            if (Date.now() >= expiresAt) {
                setOAuthModalMessage(message, 'This device code has expired. Close this dialog and start again.', true);
                return;
            }
            try {
                const result = await oauthResponseData(
                    await fetch(`/api/oauth/${encodeURIComponent(providerId)}/poll`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            deviceCode: device.device_code,
                            codeVerifier: device.codeVerifier,
                            extraData: device.extraData || {},
                        }),
                    }),
                    'Device authorization poll failed'
                );
                if (result.success && result.connection) {
                    await completeOAuthConnection(providerId, result.connection, modal);
                    return;
                }
                if (!result.pending) {
                    setOAuthModalMessage(message, oauthErrorMessage(result, 'Device authorization failed.'), true);
                    return;
                }
                setOAuthModalMessage(message, 'Waiting for approval…');
            } catch (error) {
                setOAuthModalMessage(message, error.message || 'Device authorization poll failed.', true);
                return;
            }
            if (!cancelled && modal.overlay.isConnected) pollTimer = setTimeout(poll, interval);
        };
        pollTimer = setTimeout(poll, interval);
    } catch (error) {
        if (modal.overlay.isConnected) {
            modal.content.innerHTML = `<div style="text-align:center;padding:18px 8px;color:var(--danger);font-size:14px;line-height:1.5;"></div>`;
            modal.content.firstElementChild.textContent = error.message || 'Could not start device authorization.';
        }
    }
}

function showKiroModeSelector() {
    const modal = showOAuthModal({
        title: 'Connect Kiro AI',
        body: `<div style="display:flex;flex-direction:column;gap:10px;padding:8px;">
            <p style="margin:0 0 4px;font-size:14px;color:var(--text-muted);">Choose your authentication method</p>
            <button id="kiro-mode-builder" class="btn btn-outline" type="button" style="display:flex;flex-direction:column;gap:4px;padding:13px;text-align:left;">
                <span style="font-weight:600;font-size:14px;">AWS Builder ID</span>
                <span style="font-size:12px;opacity:.8;">Recommended for most users. Free AWS account required.</span>
            </button>
            <button id="kiro-mode-idc" class="btn btn-outline" type="button" style="display:flex;flex-direction:column;gap:4px;padding:13px;text-align:left;">
                <span style="font-weight:600;font-size:14px;">AWS IAM Identity Center</span>
                <span style="font-size:12px;opacity:.8;">For enterprise users with custom AWS IAM Identity Center.</span>
            </button>
            <button id="kiro-mode-api-key" class="btn btn-outline" type="button" style="display:flex;flex-direction:column;gap:4px;padding:13px;text-align:left;">
                <span style="font-weight:600;font-size:14px;">API Key</span>
                <span style="font-size:12px;opacity:.8;">Use a long-lived Kiro/CodeWhisperer API key (headless auth).</span>
            </button>
            <button id="kiro-mode-import-token" class="btn btn-outline" type="button" style="display:flex;flex-direction:column;gap:4px;padding:13px;text-align:left;">
                <span style="font-weight:600;font-size:14px;">Import Token</span>
                <span style="font-size:12px;opacity:.8;">Paste refresh token from Kiro IDE.</span>
            </button>
            <button id="kiro-mode-import-cliproxy" class="btn btn-outline" type="button" style="display:flex;flex-direction:column;gap:4px;padding:13px;text-align:left;">
                <span style="font-weight:600;font-size:14px;">Import CLIProxyAPI JSON</span>
                <span style="font-size:12px;opacity:.8;">Paste external_idp auth JSON from CLIProxyAPI/Kiro Microsoft login.</span>
            </button>
        </div>`
    });
    modal.content.querySelector('#kiro-mode-builder').onclick = () => { modal.close(); startDeviceCodeFlow('kiro', new URLSearchParams({ auth_method: 'builder-id' })); };
    modal.content.querySelector('#kiro-mode-idc').onclick = () => renderKiroForm(modal, 'idc');
    modal.content.querySelector('#kiro-mode-api-key').onclick = () => renderKiroForm(modal, 'api-key');
    modal.content.querySelector('#kiro-mode-import-token').onclick = () => renderKiroForm(modal, 'token');
    modal.content.querySelector('#kiro-mode-import-cliproxy').onclick = () => renderKiroForm(modal, 'cliproxy');
}

async function startKiroSocialAuth(provider) {
    const name = provider === 'google' ? 'Google' : 'GitHub';
    let pollTimer = null;
    let cancelled = false;
    const modal = showOAuthModal({
        title: `Sign in with ${name}`,
        body: `<div style="text-align:center;padding:24px;"><div style="width:24px;height:24px;margin:0 auto 14px;border:2px solid var(--border-color);border-top-color:var(--brand-color);border-radius:50%;animation:oauth-spin .8s linear infinite;"></div><div style="font-size:14px;color:var(--text-muted);">Preparing sign-in URL…</div></div>`,
        onClose: () => { cancelled = true; if (pollTimer) clearTimeout(pollTimer); }
    });
    try {
        const auth = await oauthResponseData(
            await fetch(`/api/oauth/kiro/social-authorize?provider=${encodeURIComponent(provider)}`),
            'Could not start Kiro social authorization'
        );
        if (!modal.overlay.isConnected) return;
        modal.content.innerHTML = `
            <div style="display:flex;flex-direction:column;align-items:center;gap:16px;text-align:center;">
                <p style="margin:0;font-size:14px;line-height:1.5;color:var(--text-muted);">Open the link below, sign in with ${name}, then paste the callback URL here.</p>
                <a id="kiro-social-link" class="btn btn-primary" target="_blank" rel="noopener noreferrer" href="${escapeOAuthHtml(auth.authUrl)}" style="text-decoration:none;">Open sign-in page</a>
                <input id="kiro-callback-input" class="input" type="url" placeholder="kiro://kiro.kiroAgent/authenticate-success?code=..." style="width:100%;" />
                <div id="kiro-social-message" style="display:none;font-size:12px;"></div>
                <button id="kiro-social-exchange" class="btn btn-primary" type="button">Finish connection</button>
            </div>`;
        const message = modal.content.querySelector('#kiro-social-message');
        const exchangeBtn = modal.content.querySelector('#kiro-social-exchange');
        exchangeBtn.onclick = async () => {
            const callback = modal.content.querySelector('#kiro-callback-input').value.trim();
            if (!callback) { setOAuthModalMessage(message, 'Paste the callback URL from the browser.', true); return; }
            let url;
            try { url = new URL(callback); } catch { setOAuthModalMessage(message, 'Invalid URL. Paste the full callback URL.', true); return; }
            const code = url.searchParams.get('code');
            if (!code) { setOAuthModalMessage(message, 'No authorization code found in the callback URL.', true); return; }
            exchangeBtn.disabled = true;
            exchangeBtn.textContent = 'Connecting…';
            try {
                const result = await oauthResponseData(
                    await fetch('/api/oauth/kiro/social-exchange', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ code, codeVerifier: auth.codeVerifier, provider }),
                    }),
                    'Kiro social exchange failed'
                );
                if (!result.success || !result.connection) throw new Error(oauthErrorMessage(result, 'Kiro connection failed.'));
                await completeOAuthConnection('kiro', result.connection, modal);
            } catch (error) {
                setOAuthModalMessage(message, error.message || 'Kiro connection failed.', true);
                exchangeBtn.disabled = false;
                exchangeBtn.textContent = 'Finish connection';
            }
        };
        modal.content.querySelector('#kiro-callback-input').focus();
    } catch (error) {
        if (modal.overlay.isConnected) {
            modal.content.innerHTML = `<div style="text-align:center;padding:18px 8px;color:var(--danger);font-size:14px;line-height:1.5;"></div>`;
            modal.content.firstElementChild.textContent = error.message || 'Could not start Kiro social authorization.';
        }
    }
}

function renderKiroForm(modal, mode) {
    const forms = {
        idc: ['AWS IAM Identity Center', `<label>Your organization's AWS IAM Identity Center URL<input id="kiro-start-url" class="input" value="https://view.awsapps.com/start" required></label><label>AWS Region<input id="kiro-region" class="input" value="us-east-1" required><span style="font-size:11px;color:var(--text-muted);">AWS region for your Identity Center (default: us-east-1)</span></label>`, 'Continue'],
        'api-key': ['API Key', `<label>API Key<input id="kiro-value" class="input" type="password" autocomplete="off" required></label><label>AWS Region<input id="kiro-region" class="input" value="us-east-1" required><span style="font-size:11px;color:var(--text-muted);">AWS region for your Identity Center (default: us-east-1)</span></label>`, 'Import'],
        token: ['Import Token', `<label>Refresh Token<textarea id="kiro-value" class="input" rows="4" placeholder="aorAAAAAG..." required></textarea></label><button id="kiro-auto-import" class="btn btn-outline" type="button" style="align-self:flex-start;">Import from Kiro IDE</button>`, 'Import'],
        cliproxy: ['Import CLIProxyAPI JSON', `<label>CLIProxyAPI JSON<textarea id="kiro-value" class="input" rows="6" placeholder='{"accessToken":"...","refreshToken":"..."}' required></textarea></label>`, 'Import'],
    };
    const [title, fields, action] = forms[mode];
    modal.overlay.querySelector('h3').textContent = title;
    modal.content.innerHTML = `<form id="kiro-form" style="display:flex;flex-direction:column;gap:14px;">${fields}<div id="kiro-message" style="display:none;font-size:12px;"></div><button class="btn btn-primary" type="submit">${action}</button></form>`;
    const form = modal.content.querySelector('#kiro-form');
    const message = modal.content.querySelector('#kiro-message');
    const auto = modal.content.querySelector('#kiro-auto-import');
    if (auto) auto.onclick = async () => {
        try {
            const found = await oauthResponseData(await fetch('/api/oauth/kiro/auto-import'), 'Kiro auto-import failed');
            if (!found.found) throw new Error(found.error || 'Kiro token not found.');
            modal.content.querySelector('#kiro-value').value = found.refreshToken;
            setOAuthModalMessage(message, `Found ${found.source}.`);
        } catch (error) { setOAuthModalMessage(message, error.message, true); }
    };
    form.onsubmit = async event => {
        event.preventDefault();
        const button = form.querySelector('button[type="submit"]');
        if (mode === 'idc') {
            modal.close();
            const query = new URLSearchParams({ auth_method: 'idc', start_url: form.querySelector('#kiro-start-url').value.trim(), region: form.querySelector('#kiro-region').value.trim() });
            startDeviceCodeFlow('kiro', query);
            return;
        }
        button.disabled = true;
        button.textContent = 'Connecting…';
        try {
            const value = form.querySelector('#kiro-value').value.trim();
            const region = form.querySelector('#kiro-region')?.value.trim();
            let route, body;
            if (mode === 'api-key') { route = 'api-key'; body = { apiKey: value, region }; }
            else if (mode === 'cliproxy') { route = 'cliproxy-import'; body = { json: value }; }
            else { route = 'import'; body = { refreshToken: value }; }
            const result = await oauthResponseData(await fetch(`/api/oauth/kiro/${route}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }), `Kiro ${title} failed`);
            if (!result.success || !result.connection) throw new Error(oauthErrorMessage(result, 'Kiro connection failed.'));
            await completeOAuthConnection('kiro', result.connection, modal);
        } catch (error) {
            setOAuthModalMessage(message, error.message || 'Kiro connection failed.', true);
            button.disabled = false;
            button.textContent = action;
        }
    };
}

function startKiroDeviceFlow(authMethod, startUrl = '', region = '') {
    const query = new URLSearchParams({ auth_method: authMethod });
    if (startUrl) query.set('start_url', startUrl);
    if (region) query.set('region', region);
    startDeviceCodeFlow('kiro', query);
}

// OAuth login — every displayed provider must use a backend 9router-compatible flow.
window.openOAuthTokenModal = () => {
    // Kiro consolidation: single tile, mode selector for device vs import.
    if (activeProviderId === 'kiro') {
        showKiroModeSelector();
        return;
    }
    const flowType = OAUTH_FLOW_TYPES[activeProviderId];
    const providerConfig = OAUTH_PROVIDER_CONFIG[activeProviderId] || {};
    if (flowType === 'device_code') {
        startDeviceCodeFlow(activeProviderId);
        return;
    }
    if (flowType === 'import_token') {
        startNativeTokenImport(activeProviderId);
        return;
    }
    if (flowType === 'authorization_code' || flowType === 'authorization_code_pkce' || flowType === 'browser_token') {
        if (providerConfig.fixedPort && (activeProviderId === 'openai_codex' || activeProviderId === 'codex')) {
            startCodexLoopback();
        } else {
            startAuthCodeFlow(activeProviderId);
        }
        return;
    }
    showToast(`OAuth flow unavailable for ${getDisplayName(activeProviderId)}`, 'error');
};

// Verify API key against provider endpoint
window.verifyConnectionKey = async (btn) => {
    const key = document.getElementById('conn-key').value.trim();
    if (!key) { alert('Please enter an API key first.'); return; }

    // Resolve the effective base URL through the same inheritance chain used
    // when saving a connection, so verification never fails just because the
    // custom URL field is blank but a URL is inherited from a sibling /
    // known provider / provider-level base_url.
    const idx = editingConnIdx >= 0 ? editingConnIdx : 0;
    let baseUrl = resolveBaseUrl(activeProviderId, idx, editingConnIdx);
    const providerFormat = globalConfig.providers[activeProviderId]?.format || 'openai';
    if (isCustomTextFormat(providerFormat)) {
        const result = normalizeUrlField('conn-url', 'conn-url-notice', providerFormat, true);
        if (result.error) return;
        baseUrl = result.value || baseUrl;
        if (baseUrl) {
            const inherited = normalizeCustomTextBaseUrl(baseUrl);
            if (inherited.error) {
                setUrlFieldNotice('conn-url', 'conn-url-notice', inherited);
                return;
            }
            baseUrl = inherited.value;
        }
    }

    const origText = btn.textContent;
    btn.textContent = 'Checking...';
    btn.disabled = true;

    try {
        const res = await fetch('/api/verify-key', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider_id: activeProviderId, format: providerFormat, api_key: key, base_url: baseUrl })
        });
        const data = await res.json();
        if (data.ok) {
            btn.textContent = '✓ Valid';
            btn.style.color = 'var(--success)';
        } else {
            btn.textContent = '✗ Invalid';
            btn.style.color = 'var(--danger)';
        }
    } catch {
        btn.textContent = '? Error';
        btn.style.color = 'var(--text-muted)';
    } finally {
        btn.disabled = false;
        setTimeout(() => { btn.textContent = origText; btn.style.color = ''; }, 3000);
    }
};

let editingConnIdx = -1;
window.openConnModal = (idx = -1) => {
    editingConnIdx = idx;
    document.getElementById('add-conn-modal').classList.add('active');
    
    const pConfig = globalConfig.providers[activeProviderId];
    const isCustom = pConfig.type === 'custom';
    const knownProvider = KNOWN_PROVIDERS.api.find(p => p.id === activeProviderId)
                       || KNOWN_PROVIDERS.oauth.find(p => p.id === activeProviderId);

    const urlGroup = document.getElementById('conn-url-group');
    const hintEl = document.getElementById('conn-url-hint');

    // Pre-compute the inheritance chain for custom providers so the URL field
    // can pre-fill from an existing sibling/known URL and stay optional when
    // a provider-level default already exists.
    const firstConnWithUrl = (pConfig.connections || []).find(c => c && c.base_url);
    const inheritedUrl = (firstConnWithUrl && firstConnWithUrl.base_url)
                      || (knownProvider && knownProvider.url)
                      || pConfig.base_url || '';

    if (isCustom) {
        // Custom provider: URL field is editable but OPTIONAL — inherit when blank.
        urlGroup.style.display = '';
        hintEl.innerHTML = (inheritedUrl
            ? `<span style="color:var(--text-muted);font-weight:400;">(leave blank to inherit: ${inheritedUrl})</span>`
            : '<span style="color:var(--danger)">*</span>');
    } else {
        // Known provider: URL is hardcoded — hide the field entirely
        urlGroup.style.display = 'none';
        hintEl.innerHTML = '';
    }

    if (idx >= 0) {
        const conn = pConfig.connections[idx];
        document.getElementById('conn-name').value = conn.name || '';
        // Normalize legacy custom text values for display only. Config changes on save.
        document.getElementById('conn-url').value = conn.base_url || '';
        if (isCustomTextFormat(pConfig.format) && conn.base_url) {
            normalizeUrlField('conn-url', 'conn-url-notice', pConfig.format, false);
        } else {
            setUrlFieldNotice('conn-url', 'conn-url-notice', { corrected: false, error: '' });
        }
        document.getElementById('conn-key').value = conn.api_key || '';
        document.getElementById('conn-proxy').value = conn.proxy_url || '';
        document.getElementById('conn-modal-title').textContent = 'Edit Connection';
        document.querySelector('#add-conn-modal .modal-footer .btn-primary').textContent = 'Save Changes';
    } else {
        document.getElementById('conn-name').value = 'Connection ' + (pConfig.connections.length + 1);
        // Start blank for new connections so inheritance kicks in on save.
        document.getElementById('conn-url').value = '';
        setUrlFieldNotice('conn-url', 'conn-url-notice', { corrected: false, error: '' });
        document.getElementById('conn-key').value = '';
        document.getElementById('conn-proxy').value = '';
        document.getElementById('conn-modal-title').textContent = 'Add Connection';
        document.querySelector('#add-conn-modal .modal-footer .btn-primary').textContent = 'Add Connection';
    }
    updateCustomUrlHelp();
};

window.closeConnModal = () => document.getElementById('add-conn-modal').classList.remove('active');

window.saveConnection = () => {
    const name = document.getElementById('conn-name').value.trim();
    const key = document.getElementById('conn-key').value.trim();
    const proxyUrl = document.getElementById('conn-proxy').value.trim();

    const pConfig = globalConfig.providers[activeProviderId];
    const isCustom = pConfig.type === 'custom';

    // Resolve effective base URL using the inheritance chain so the user is
    // never forced to re-enter a URL the provider/connection already owns:
    //   1. value typed in the modal field
    //   2. existing sibling connection base_url
    //   3. known provider URL
    //   4. provider-level base_url (legacy/custom)
    let url = '';
    if (isCustom) {
        const normalized = normalizeUrlField('conn-url', 'conn-url-notice', pConfig.format, true);
        if (normalized.error) return;
        url = normalized.value;
        if (!url) {
            // Inherit from siblings / known provider / provider-level base_url
            // so the user is never forced to re-enter it. Persist the inherited
            // value on the connection so the backend routes consistently.
            url = resolveBaseUrl(activeProviderId, editingConnIdx >= 0 ? editingConnIdx : (pConfig.connections || []).length, editingConnIdx);
            if (!url) {
                alert('Base URL is required for custom providers. Add one to the first connection or set a provider base URL.');
                return;
            }
            if (isCustomTextFormat(pConfig.format)) {
                const inherited = normalizeCustomTextBaseUrl(url);
                if (inherited.error) {
                    setUrlFieldNotice('conn-url', 'conn-url-notice', inherited);
                    return;
                }
                url = inherited.value;
            }
        }
    }
    // For known providers: don't store URL in connection — backend resolves from PROVIDER_DEFAULT_URLS

    if (editingConnIdx >= 0) {
        pConfig.connections[editingConnIdx].name = name;
        if (isCustom) pConfig.connections[editingConnIdx].base_url = url;
        pConfig.connections[editingConnIdx].api_key = key;
        pConfig.connections[editingConnIdx].proxy_url = proxyUrl;
    } else {
        const conn = { name, api_key: key, enabled: true };
        if (isCustom) conn.base_url = url;
        if (proxyUrl) conn.proxy_url = proxyUrl;
        pConfig.connections.push(conn);
    }
    saveConfig();
    closeConnModal();
    renderActiveTab();
};

let editingProviderId = null;
window.openProviderModal = (type = 'text', providerId = null) => {
    editingProviderId = providerId || null;
    const modal = document.getElementById('add-provider-modal');
    modal.classList.add('active');

    const nameInput = document.getElementById('p-name');
    const prefixInput = document.getElementById('p-prefix');
    const formatInput = document.getElementById('p-format');
    const urlInput = document.getElementById('p-url');
    const keyInput = document.getElementById('p-key');
    const titleEl = document.querySelector('#add-provider-modal .modal-header h3');
    const saveBtn = document.getElementById('provider-modal-save-btn');

    nameInput.value = '';
    prefixInput.value = '';
    prefixInput.disabled = false;
    urlInput.value = '';
    keyInput.value = '';
    setUrlFieldNotice('p-url', 'p-url-notice', { corrected: false, error: '' });

    if (editingProviderId && globalConfig.providers && globalConfig.providers[editingProviderId]) {
        const p = globalConfig.providers[editingProviderId];
        const firstConn = (p.connections || [])[0] || {};
        if (titleEl) titleEl.textContent = `Edit Provider: ${p.name || editingProviderId}`;
        if (saveBtn) saveBtn.textContent = 'Save Config';
        nameInput.value = p.name || editingProviderId;
        prefixInput.value = editingProviderId;
        prefixInput.disabled = false;
        formatInput.value = p.format || 'openai';
        urlInput.value = firstConn.base_url || '';
        if (isCustomTextFormat(formatInput.value) && firstConn.base_url) {
            normalizeUrlField('p-url', 'p-url-notice', formatInput.value, false);
        }
        keyInput.value = firstConn.api_key || '';
        updateCustomUrlHelp();
        return;
    }

    if (titleEl) {
        if (type === 'image') titleEl.textContent = 'Add Custom Image Provider';
        else if (type === 'video') titleEl.textContent = 'Add Custom Video Provider';
        else titleEl.textContent = 'Add Custom Provider';
    }
    if (saveBtn) saveBtn.textContent = 'Create Provider';
    if (type === 'image') formatInput.value = 'openai-image';
    else if (type === 'video') formatInput.value = 'openai-video';
    else formatInput.value = 'openai';
    updateCustomUrlHelp();
};
window.closeProviderModal = () => {
    editingProviderId = null;
    document.getElementById('add-provider-modal').classList.remove('active');
};
window.saveProviderModal = () => {
    const name = document.getElementById('p-name').value.trim();
    const rawProviderId = document.getElementById('p-prefix').value.trim();
    const format = document.getElementById('p-format').value;
    const normalizedUrl = normalizeUrlField('p-url', 'p-url-notice', format, false);
    if (normalizedUrl.error) return;
    const url = normalizedUrl.value || document.getElementById('p-url').value.trim();
    const key = document.getElementById('p-key').value.trim();

    if (!name) {
        alert("Provider Name is required.");
        return;
    }

    // The UI label says "Provider Prefix", but operationally this value is the
    // provider key used everywhere in config.yaml (providers, aliases, combos).
    // Older code concatenated prefix + name when creating providers and ignored
    // this field entirely while editing, which made edits appear saved but then
    // revert to generated IDs like "vietapivietapi".
    const normalizedId = rawProviderId
        .toLowerCase()
        .replace(/[^a-z0-9_-]+/g, '-')
        .replace(/^-+|-+$/g, '');

    if (!normalizedId) {
        alert("Provider Prefix / ID is required. Example: vietapi-o");
        return;
    }

    const oldId = editingProviderId;
    const id = normalizedId;

    if (oldId && oldId !== id && globalConfig.providers[id]) {
        alert(`Provider ID "${id}" already exists. Choose a unique ID.`);
        return;
    }
    if (!oldId && globalConfig.providers[id]) {
        alert(`Provider ID "${id}" already exists. Choose a unique ID.`);
        return;
    }

    let type = 'custom';
    if (format.endsWith('-image')) type = 'image_custom';
    else if (format.endsWith('-video')) type = 'video_custom';

    if (oldId && oldId !== id) {
        globalConfig.providers[id] = globalConfig.providers[oldId];
        delete globalConfig.providers[oldId];

        // Keep all provider references consistent after a key rename.
        if (globalConfig.aliases) {
            Object.values(globalConfig.aliases).forEach(alias => {
                if (alias && alias.provider === oldId) alias.provider = id;
            });
        }
        if (Array.isArray(globalConfig.combos)) {
            globalConfig.combos.forEach(combo => {
                if (!Array.isArray(combo.chain)) return;
                combo.chain.forEach(step => {
                    if (step && step.provider === oldId) step.provider = id;
                });
            });
        }
        if (globalConfig.agent && globalConfig.agent.provider === oldId) {
            globalConfig.agent.provider = id;
        }
        if (activeProviderId === oldId) activeProviderId = id;
    }

    if (!globalConfig.providers[id]) {
        globalConfig.providers[id] = { type, format, name, connections: [], models: [] };
    } else {
        globalConfig.providers[id].name = name;
        globalConfig.providers[id].format = format;
        globalConfig.providers[id].type = type;
    }

    if (url || key) {
        if (!globalConfig.providers[id].connections) globalConfig.providers[id].connections = [];
        const firstConn = globalConfig.providers[id].connections[0] || { name: 'Primary Connection', enabled: true };
        firstConn.base_url = url;
        firstConn.api_key = key;
        if (firstConn.enabled === undefined) firstConn.enabled = true;
        globalConfig.providers[id].connections[0] = firstConn;
    }

    closeProviderModal();
    showProviderDetail(id);
    saveConfig();
};

function renderEndpointTab() {
    const host = window.location.host || '127.0.0.1:6969';
    const proto = window.location.protocol || 'http:';
    
    return `
    <div class="detail-card" style="border-radius:12px;border:1px solid var(--border-color);margin-bottom:24px;">
        <div class="detail-card-header" style="padding-bottom:12px;">
            <h2 style="font-size:15px;">Local Endpoints</h2>
            <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">Use these URLs in your applications to route traffic through BSL Router.</div>
        </div>
        <div style="padding:16px;">
            <div style="display:flex; align-items:center; justify-content:space-between; padding:12px 0;">
                <div style="font-size:13px; font-weight:600;">OpenAI & Anthropic Compatible</div>
                <div style="display:flex; gap:8px;">
                    <code style="background:#f3f4f6; padding:4px 8px; border-radius:4px; font-size:12px; color:var(--text-main);">${proto}//${host}/v1</code>
                    <button class="btn btn-outline" style="padding:4px 8px;" onclick="navigator.clipboard.writeText('${proto}//${host}/v1')">Copy</button>
                </div>
            </div>
        </div>
    </div>

    <div class="detail-card" style="border-radius:12px;border:1px solid var(--border-color);margin-bottom:24px;">
        <div class="detail-card-header" style="padding-bottom:12px;">
            <h2 style="font-size:15px;">Remote Access & Sharing</h2>
            <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">Expose your local BSL Router to external networks or other devices.</div>
        </div>
        <div style="padding:16px;">
            <div style="display:flex; align-items:center; justify-content:space-between; padding:12px 0; border-bottom:1px solid var(--border-color);">
                <div>
                    <div style="font-size:13px; font-weight:600;">Cloudflare Tunnel</div>
                    <div style="font-size:11px; color:var(--text-muted);">Secure public URL via Cloudflared</div>
                </div>
                <button class="btn btn-outline" style="padding:6px 12px; display:flex; align-items:center; gap:6px;" id="cf-tunnel-btn" onclick="toggleCloudflareTunnel(this)">
                    <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"></path></svg>
                    Start Tunnel
                </button>
            </div>
            <div style="display:flex; align-items:center; justify-content:space-between; padding:12px 0;">
                <div>
                    <div style="font-size:13px; font-weight:600;">Tailscale Network</div>
                    <div style="font-size:11px; color:var(--text-muted);">Share securely on your Tailnet</div>
                </div>
                <button class="btn btn-outline" style="padding:6px 12px; display:flex; align-items:center; gap:6px;" id="tailscale-btn" onclick="checkTailscale(this)">
                    <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><circle cx="12" cy="12" r="10"></circle><line x1="2" y1="12" x2="22" y2="12"></line><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path></svg>
                    Get Tailscale URL
                </button>
            </div>
        </div>
    </div>

    <div class="detail-card" style="border-radius:12px;border:1px solid var(--border-color);margin-bottom:24px;">
        <div class="detail-card-header" style="padding-bottom:12px; display:flex; justify-content:space-between; align-items:center;">
            <div>
                <h2 style="font-size:15px;">API Keys</h2>
                <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">Generate API keys to share access to your router.</div>
            </div>
            <button class="btn btn-primary" style="padding:6px 12px; font-size:13px;" onclick="generateAPIKey()">
                Generate New Key
            </button>
        </div>
        <div style="padding:16px;" id="api-keys-list">
            ${(globalConfig.keys || []).map((k, i) => `
            <div style="display:flex; align-items:center; justify-content:space-between; padding:12px 0; border-bottom: ${i < globalConfig.keys.length - 1 ? '1px solid var(--border-color)' : 'none'};">
                <div style="font-size:13px; font-weight:600;">Key ${i + 1}</div>
                <div style="display:flex; gap:8px;">
                    <code style="background:#f3f4f6; padding:4px 8px; border-radius:4px; font-size:12px; color:var(--text-main);">${k}</code>
                    <button class="btn btn-outline" style="padding:4px 8px;" onclick="navigator.clipboard.writeText('${k}')">Copy</button>
                    <button class="btn btn-outline" style="padding:4px 8px; color:red; border-color:red;" onclick="deleteAPIKey(${i})">Delete</button>
                </div>
            </div>`).join('') || '<div style="font-size:13px; color:var(--text-muted); text-align:center; padding:12px 0;">No keys generated</div>'}
        </div>
    </div>
    `;
}


const AG_SLOTS=[
    {key:'gemini-3.5-flash-medium',label:'Gemini 3.5 Flash (Medium)'},
    {key:'gemini-3.5-flash-high',label:'Gemini 3.5 Flash (High)'},
    {key:'gemini-3.5-flash-low',label:'Gemini 3.5 Flash (Low)'},
    {key:'gemini-3.1-pro-low',label:'Gemini 3.1 Pro (Low)'},
    {key:'gemini-3.1-pro-high',label:'Gemini 3.1 Pro (High)'},
    {key:'claude-sonnet-4-6',label:'Claude Sonnet 4.6 (Thinking)'},
    {key:'claude-opus-4-6-thinking',label:'Claude Opus 4.6 (Thinking)'},
    {key:'gpt-oss-120b-medium',label:'GPT-OSS 120B (Medium)'}
];
function agConfig(){if(!globalConfig.antigravity_integration||typeof globalConfig.antigravity_integration!=='object')globalConfig.antigravity_integration={};let c=globalConfig.antigravity_integration;if(!c.mappings||typeof c.mappings!=='object')c.mappings={};if(typeof c.enabled!=='boolean')c.enabled=false;return c;}
function agEsc(v){return String(v??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function agOptions(selected){let h='<option value="">Use native Antigravity model (unmapped)</option>',e=agEsc,cs=(globalConfig.combos||[]).filter(c=>c?.alias);if(cs.length)h+=`<optgroup label="BSL Combo aliases">${cs.map(c=>`<option value="${e(c.alias)}" ${c.alias===selected?'selected':''}>${e(c.alias)}</option>`).join('')}</optgroup>`;h+=_bslModelsOptgroupHTML(selected);Object.entries(globalConfig.providers||{}).forEach(([pid,p])=>{if(_isBlacksandProvider(pid))return;if(!isProviderSelectable(p))return;let ms=(p?.models||[]).filter(m=>m?.id&&m.enabled!==false);if(ms.length)h+=`<optgroup label="${e(getDisplayName(pid))}">${ms.map(m=>{let v=`${pid}/${m.id}`;return `<option value="${e(v)}" ${v===selected?'selected':''}>${e(m.name||m.id)}</option>`}).join('')}</optgroup>`});return h;}
function renderMitmTab(){let c=agConfig(),url=`http://127.0.0.1:${globalConfig.server?.port||6969}`,rows=AG_SLOTS.map(({key:k,label:l},index)=>{let t=c.mappings[k]||'',kind=t?(t.includes('/')?'Provider model':'Combo alias'):'Native Antigravity inference';return `<article id="ag-slot-${k}" class="ag-integration-map-card" data-ag-slot="${k}" data-ag-slot-index="${index + 1}"><div class="ag-integration-map-source"><b>${t?'✦':'A'}</b><div><strong>${agEsc(l)}</strong><code>${agEsc(k)}</code></div></div><span class="ag-integration-map-flow" aria-hidden="true">→</span><div class="ag-integration-map-target"><select id="ag-target-${k}" class="input" data-ag-target-slot="${k}" aria-label="Target for ${agEsc(l)}" onchange="updateAntigravityIntegrationMapping('${k}',this.value)">${agOptions(t)}</select><span class="ag-integration-kind ${t?'is-routed':'is-native'}">${kind}</span></div></article>`}).join('');return `<section id="ag-integration-overview" class="ag-integration-hero" data-antigravity-menu-version="2.1.1"><div><span>DIRECT INFERENCE OVERLAY</span><h2>Antigravity Integration</h2><p>Mapped slots use BSL Router. Unmapped slots use native Google Cloud Code only when Antigravity forwards Google credentials.</p><small class="ag-integration-version" id="ag-menu-version">Antigravity IDE 2.1.1 model menu</small></div><strong id="ag-integration-runtime-label">${c.enabled?'CHECKING…':'STOPPED'}</strong></section><section class="detail-card ag-integration-control-card"><div><h3>BSL direct endpoint</h3><p>Configure Antigravity's language server here. No port 443, CA, hosts-file, or DNS interception is used.</p><code>${url}</code></div><button id="ag-integration-toggle" class="btn ${c.enabled?'btn-danger':'btn-primary'}" onclick="toggleAntigravityIntegration(${c.enabled?'false':'true'})">${c.enabled?'Stop Integration':'Start Integration'}</button></section><div class="ag-integration-diagnostics" id="ag-integration-diagnostics">Checking direct-inference readiness…</div><section id="ag-slot-mappings" class="detail-card" data-ag-slot-count="${AG_SLOTS.length}"><div class="detail-card-header"><div><h2>Model slot mappings</h2><p>Model selection changes apply to new Antigravity conversations; Retry/Continue can retain the original conversation model.</p><p>Unmapped direct-Overlay slots require Google credentials forwarded by Antigravity; otherwise map them to a BSL target.</p><p>Choosing a native Antigravity model clears only this dedicated mapping; global aliases are never consulted.</p></div><span id="ag-routed-count">${Object.keys(c.mappings).length} routed</span></div><div id="ag-integration-map-list" class="ag-integration-map-list">${rows}</div></section>`;}
window.updateAntigravityIntegrationMapping=async(k,t)=>{let c=agConfig(),had=Object.prototype.hasOwnProperty.call(c.mappings,k),previous=c.mappings[k];if(t)c.mappings[k]=t;else delete c.mappings[k];try{if(!await saveConfig())throw Error('configuration save failed');renderActiveTab();showToast(t?`Mapped ${k} through BSL Router.`:`Using native Antigravity model for ${k}.`)}catch(e){if(had)c.mappings[k]=previous;else delete c.mappings[k];renderActiveTab();showToast(`Failed to save Antigravity mapping: ${e.message||'request failed'}`,true);};};
window.toggleAntigravityIntegration=async(enabled)=>{let b=document.getElementById('ag-integration-toggle');if(b)b.disabled=true;try{let r=await fetch(enabled?'/api/antigravity-integration/start-full':'/api/antigravity-integration/stop-full',{method:'POST'}),d=await r.json().catch(()=>({}));if(!r.ok||d.ok!==true||d.enabled!==enabled)throw Error(d.error||'Backend state did not match the requested state.');agConfig().enabled=enabled;renderActiveTab();showToast(d.message||`Antigravity integration ${enabled?'started':'stopped'}.`)}catch(e){showToast(`Antigravity integration error: ${e.message}`,true);await pollAntigravityIntegrationStatus()}finally{if(b)b.disabled=false}};
let agPollTimer=null;function renderAntigravityIntegrationStatus(s){let on=s?.enabled===true,l=document.getElementById('ag-integration-runtime-label'),d=document.getElementById('ag-integration-diagnostics'),b=document.getElementById('ag-integration-toggle');if(l)l.textContent=on?'RUNNING':s?.state==='stopped'?'STOPPED':'STATUS UNKNOWN';if(d){d.textContent=s?.diagnostics||'Direct-inference status is unavailable. Confirm BSL Router is reachable.';d.classList.toggle('is-ready',on)}if(b&&!b.disabled){b.textContent=on?'Stop Integration':'Start Integration';b.className=`btn ${on?'btn-danger':'btn-primary'}`;b.setAttribute('onclick',`toggleAntigravityIntegration(${on?'false':'true'})`)}};
async function pollAntigravityIntegrationStatus(){try{let r=await fetch('/api/antigravity-integration/status',{cache:'no-store'}),s=await r.json();if(!r.ok)throw Error(s.error||'status request failed');agConfig().enabled=!!s.enabled;renderAntigravityIntegrationStatus(s);return s}catch(e){renderAntigravityIntegrationStatus(null);return null}}
function startAntigravityIntegrationPolling(){stopAntigravityIntegrationPolling();pollAntigravityIntegrationStatus();agPollTimer=setInterval(()=>{if(document.querySelector('.nav-item.active')?.dataset?.tab==='mitm')pollAntigravityIntegrationStatus()},4000)}
function stopAntigravityIntegrationPolling(){if(agPollTimer)clearInterval(agPollTimer);agPollTimer=null}

window.updateAlias = (key, value) => {
    if (!globalConfig.aliases) globalConfig.aliases = {};
    const rawValue = String(value || '').trim();
    if (rawValue === '') {
        delete globalConfig.aliases[key];
    } else {
        const comboAliases = new Set((globalConfig.combos || []).map(c => c?.alias).filter(Boolean));
        let provider = '__combo__';
        let targetModel = rawValue;

        if (rawValue.includes('/')) {
            const parts = rawValue.split('/');
            provider = parts.shift();
            targetModel = parts.join('/');
        } else if (!comboAliases.has(rawValue)) {
            provider = '';
        }

        globalConfig.aliases[key] = {
            provider: provider,
            model: targetModel,
            type: "chat"
        };
    }
    saveConfig();
    renderActiveTab();
};

window.updateMitmConfig = (key, enabled) => {
    if (!globalConfig.mitm) globalConfig.mitm = {};
    globalConfig.mitm[key] = enabled;
    saveConfig();
};

window.toggleMasterMITM = async (enabled) => {
    if (!globalConfig.mitm) globalConfig.mitm = {};

    showToast(enabled ? 'Starting BSL MITM: clearing port ownership...' : 'Stopping and verifying BSL MITM...');
    const btn = document.getElementById('mitm-master-toggle');
    if (btn) { btn.disabled = true; btn.dataset.requestPending = 'true'; btn.style.opacity = '0.6'; }

    try {
        // Stop uses force=true: it sets desired_running=False BEFORE killing, so
        // the watchdog cannot respawn mitmdump, then raw-taskkills every listener
        // on the port. Without force the request hits the ownership gate, so a
        // zombie mitmdump from a previous crash survives "Stop Integration" and
        // keeps holding :443.
        const res = await fetch(
            enabled ? '/api/mitm/start' : '/api/mitm/stop?force=true',
            { method: 'POST' },
        );
        const data = await res.json().catch(() => ({}));
        const verified = data.ok === true
            && data.server === enabled
            && (enabled ? data.conflict === false : data.port_occupied === false);
        if (!res.ok || !verified) {
            const detail = data.error || formatMitmConflict(data) || 'backend state did not match the requested state';
            showToast('MITM error: ' + detail, true);
            return;
        }

        globalConfig.mitm.enabled = enabled;
        if (!enabled) {
            const dnsRemoved = await removeManagedMitmHosts();
            for (const ide of dnsRemoved) globalConfig.mitm[ide] = false;
        }
        await saveConfig();
        renderActiveTab();
        showToast(data.message || (enabled ? 'BSL MITM verified running.' : 'BSL MITM verified stopped.'));
    } catch (e) {
        showToast('MITM request failed: ' + e.message, true);
    } finally {
        await pollMitmStatus();
        if (btn) { delete btn.dataset.requestPending; btn.disabled = btn.dataset.statusUnknown === 'true'; btn.style.opacity = '1'; }
    }
};

async function removeManagedMitmHosts() {
    const removed = [];
    for (const ide of ['antigravity', 'copilot', 'kiro']) {
        try {
            const res = await fetch('/api/mitm/hosts', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ide, action: 'remove' }),
            });
            const data = await res.json().catch(() => ({}));
            if (res.ok && data.ok) removed.push(ide);
            else showToast(`MITM stopped, but ${ide} DNS entries could not be removed: ${data.error || 'unknown error'}`, true);
        } catch (e) {
            showToast(`MITM stopped, but ${ide} DNS entries could not be removed: ${e.message}`, true);
        }
    }
    return removed;
}

function formatMitmConflict(status) {
    if (status?.inspection_error || status?.state === 'unknown') {
        return status.inspection_error || 'MITM port state is unknown';
    }
    if (!status?.conflict) return '';
    const owners = (status.owners || []).filter(owner => !owner.is_bsl_mitm);
    if (!owners.length) return 'MITM port conflict detected';
    return 'MITM port conflict: ' + owners.map(owner => `${owner.name || 'unknown'} (PID ${owner.pid ?? '?'})`).join(', ');
}

function formatMitmOwner(owner) {
    const ancestry = (owner.parent_chain || []).map(parent => `${parent.name || 'unknown'}:${parent.pid ?? '?'}`).join(' <- ');
    const base = `${owner.name || 'unknown'} (PID ${owner.pid ?? '?'})${owner.is_bsl_mitm ? ' [BSL]' : ' [FOREIGN]'}`;
    return ancestry ? `${base} <- ${ancestry}` : base;
}

function renderMitmRuntimeStatus(status) {
    const label = document.getElementById('mitm-runtime-label');
    const diagnostics = document.getElementById('mitm-runtime-diagnostics');
    const btn = document.getElementById('mitm-master-toggle');
    const conflict = formatMitmConflict(status);
    const unknown = status?.state === 'unknown' || Boolean(status?.inspection_error);
    const stateLabels = {
        'owned': 'OWNED',
        'foreign-owned': 'FOREIGN OWNED',
        'ownership-lost': 'OWNERSHIP LOST',
        'stopped': 'STOPPED',
        'unknown': 'STATUS UNKNOWN',
    };
    const healthy = status?.state === 'owned' && status?.ownership_verified === true;
    if (label) {
        label.textContent = stateLabels[status?.state] || String(status?.state || 'UNKNOWN').toUpperCase();
        label.title = conflict || status?.transition || '';
        label.style.background = healthy ? '#e6f4ea' : '#fce8e6';
        label.style.color = healthy ? '#1e8e3e' : '#d93025';
    }
    if (diagnostics) {
        const owners = (status?.owners || []).map(formatMitmOwner);
        const lines = [
            `port=:${status?.port ?? 443} state=${status?.state || 'unknown'} desired=${Boolean(status?.desired_running)}`,
            `verified=${Boolean(status?.ownership_verified)} tracked_pid=${status?.tracked_pid ?? 'none'} observed=${status?.observed_at ? new Date(status.observed_at * 1000).toLocaleTimeString() : 'unknown'}`,
            status?.inspection_error ? `inspection_error=${status.inspection_error}` : `owners=${owners.length ? owners.join(' | ') : 'none'}`,
        ];
        diagnostics.textContent = lines.join('\n');
        diagnostics.style.borderColor = healthy ? '#a8dab5' : '#f3b7b3';
        diagnostics.style.background = healthy ? '#f2fbf4' : '#fff7f6';
    }
    if (btn) {
        btn.dataset.statusUnknown = unknown ? 'true' : 'false';
        if (unknown) {
            btn.disabled = true;
        } else if (!btn.dataset.requestPending) {
            btn.disabled = false;
            btn.style.opacity = '1';
            // The button reflects the ACTUAL process state, not config/desired state.
            // Key on the reconcile STATE ('owned'), not ownership_verified alone:
            // after a successful start, server=true can precede ownership_verified
            // by a reconcile cycle; keying on the flag would flash "Start Server"
            // while BSL is already bound, and pressing it would kill our own
            // just-started mitmdump (audit HIGH-1).
            const isRunning = status.state === 'owned';
            const isForeign = status?.state === 'foreign-owned';
            const isLost = status?.state === 'ownership-lost';
            btn.setAttribute('onclick', `toggleMasterMITM(${isRunning ? 'false' : 'true'})`);
            if (isRunning) {
                btn.innerHTML = '&#9209; Stop Server';
                btn.style.borderColor = '#d93025';
                btn.style.color = '#d93025';
            } else if (isForeign) {
                btn.innerHTML = '&#9888; Start Server';
                btn.title = 'Port 443 is held by a foreign process. Click to force-kill and start BSL MITM.';
                btn.style.borderColor = '#e8710a';
                btn.style.color = '#e8710a';
            } else if (isLost) {
                btn.innerHTML = '&#9888; Start Server';
                btn.title = 'BSL MITM process died. Click to restart.';
                btn.style.borderColor = '#e8710a';
                btn.style.color = '#e8710a';
            } else {
                btn.innerHTML = '&#9654; Start Server';
                btn.style.borderColor = '#1e8e3e';
                btn.style.color = '#1e8e3e';
            }
        }
    }
}

// TODO 1 (done): toggleIdeDns — wraps updateMitmConfig + auto-edits hosts file
window.toggleIdeDns = async (ide, enabled) => {
    // DNS enablement is gated by a fresh authoritative runtime check, never by
    // persisted config. This prevents the master button and DNS controls from
    // disagreeing when MITM startup or an external process changes port 443.
    if (enabled) {
        const runtime = await pollMitmStatus();
        const runtimeReady = runtime
            && runtime.state === 'owned'
            && runtime.ownership_verified === true
            && runtime.server === true
            && runtime.conflict === false
            && !runtime.inspection_error;
        if (!runtimeReady) {
            const conflict = formatMitmConflict(runtime);
            const message = !runtime
                ? 'Cannot verify the MITM Server. Check that BSL Router is reachable.'
                : runtime.state === 'unknown' || runtime.inspection_error
                    ? `Cannot verify the MITM Server: ${runtime.inspection_error || 'port state is unknown'}`
                    : conflict
                        ? `${conflict}. Start Server to let BSL take over port 443 before enabling DNS.`
                        : 'The MITM Server is not running. Start Server before enabling DNS interception.';
            alert(message);
            return;
        }
    }
    // Update config state
    updateMitmConfig(ide, enabled);
    
    // Auto-edit hosts file
    try {
        const res = await fetch('/api/mitm/hosts', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ide, action: enabled ? 'add' : 'remove' })
        });
        const data = await res.json();
        if (!data.ok) {
            if (res.status === 403) {
                alert(`⚠️ Hosts file: ${data.error}\n\nManually add:\n${ide === 'antigravity' ? '127.0.0.1 daily-cloudcode-pa.googleapis.com\n127.0.0.1 cloudcode-pa.googleapis.com' : ide === 'copilot' ? '127.0.0.1 api.individual.githubcopilot.com' : '127.0.0.1 runtime.us-east-1.kiro.dev\n127.0.0.1 q.us-east-1.amazonaws.com\n127.0.0.1 codewhisperer.us-east-1.amazonaws.com'}`);
            }
        }
    } catch (e) {
        console.warn('[BSL] Hosts file edit failed:', e);
    }
    renderActiveTab();
};

let mitmStatusPollTimer = null;

function startMitmStatusPolling() {
    if (mitmStatusPollTimer) clearInterval(mitmStatusPollTimer);
    pollMitmStatus();
    mitmStatusPollTimer = setInterval(() => {
        // Always poll — the master toggle button must reflect the actual MITM
        // process state even when the user is on another admin tab.
        pollMitmStatus();
    }, 2000);
}

function stopMitmStatusPolling() {
    if (!mitmStatusPollTimer) return;
    clearInterval(mitmStatusPollTimer);
    mitmStatusPollTimer = null;
}

async function pollMitmStatus() {
    try {
        const res = await fetch('/api/mitm/status', { cache: 'no-store' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const s = await res.json();
        const update = (sel, ok) => {
            const el = document.querySelector(sel);
            if (!el) return;
            el.style.color = ok ? '#1e8e3e' : '#d93025';
        };
        update('[data-mitm-badge="cert"]',    s.cert);
        update('[data-mitm-badge="trusted"]', s.trusted);
        update('[data-mitm-badge="server"]',  s.ownership_verified);
        renderMitmRuntimeStatus(s);
        return s;
    } catch (e) {
        renderMitmRuntimeStatus({ state: 'unknown', inspection_error: e.message, server: false, conflict: null, owners: [] });
        return null;
    }
}

// TODO 3 (done): Cloudflare Tunnel start/stop
window.toggleCloudflareTunnel = async (btn) => {
    const isRunning = btn.dataset.running === 'true';
    btn.disabled = true;
    btn.textContent = isRunning ? 'Stopping…' : 'Starting…';
    try {
        if (isRunning) {
            await fetch('/api/tunnel/cloudflare/stop', { method: 'POST' });
            btn.dataset.running = 'false';
            btn.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"></path></svg> Start Tunnel`;
            // Remove URL row if present
            const urlRow = document.getElementById('cf-tunnel-url-row');
            if (urlRow) urlRow.remove();
        } else {
            const res = await fetch('/api/tunnel/cloudflare/start', { method: 'POST' });
            const data = await res.json();
            if (data.ok) {
                btn.dataset.running = 'true';
                btn.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg> Stop Tunnel`;
                // Inject URL row
                const urlRow = document.createElement('div');
                urlRow.id = 'cf-tunnel-url-row';
                urlRow.style.cssText = 'display:flex;align-items:center;gap:8px;margin-top:8px;padding:8px;background:#f0fdf4;border:1px solid #86efac;border-radius:6px;font-size:12px;';
                urlRow.innerHTML = `<span style="color:#15803d;font-weight:600;">Tunnel URL:</span><code style="flex:1;background:transparent;color:#15803d;">${data.url}/v1</code><button class="btn btn-outline" style="padding:4px 8px;font-size:11px;" onclick="navigator.clipboard.writeText('${data.url}/v1')">Copy</button>`;
                btn.parentElement.after(urlRow);
            } else {
                alert(`Cloudflare Tunnel error: ${data.error}`);
            }
        }
    } catch (e) {
        alert(`Tunnel error: ${e.message}`);
    }
    btn.disabled = false;
};

// TODO 3 (done): Tailscale — check status and show URL
window.checkTailscale = async (btn) => {
    btn.disabled = true;
    btn.textContent = 'Checking…';
    try {
        const res = await fetch('/api/tunnel/tailscale/status');
        const data = await res.json();
        if (data.ok && data.url) {
            // Inject URL row
            const existing = document.getElementById('ts-url-row');
            if (existing) existing.remove();
            const urlRow = document.createElement('div');
            urlRow.id = 'ts-url-row';
            urlRow.style.cssText = 'display:flex;align-items:center;gap:8px;margin-top:8px;padding:8px;background:#eff6ff;border:1px solid #93c5fd;border-radius:6px;font-size:12px;';
            urlRow.innerHTML = `<span style="color:#1d4ed8;font-weight:600;">Tailscale URL:</span><code style="flex:1;background:transparent;color:#1d4ed8;">${data.url}</code><button class="btn btn-outline" style="padding:4px 8px;font-size:11px;" onclick="navigator.clipboard.writeText('${data.url}')">Copy</button>`;
            btn.parentElement.after(urlRow);
            btn.innerHTML = `<svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><circle cx="12" cy="12" r="10"></circle><polyline points="20 6 9 17 4 12"></polyline></svg> Connected`;
            btn.style.color = '#1e8e3e';
            btn.style.borderColor = '#1e8e3e';
        } else {
            alert(`Tailscale: ${data.error || 'Not connected.'}\n\nMake sure Tailscale is installed and running.`);
            btn.textContent = 'Get Tailscale URL';
        }
    } catch (e) {
        alert(`Tailscale check failed: ${e.message}`);
        btn.textContent = 'Get Tailscale URL';
    }
    btn.disabled = false;
};

// TODO 2 (done): Model Select Dialog
window.openModelSelectDialog = (aliasKey, triggerBtn) => {
    const existing = document.getElementById('model-select-overlay');
    if (existing) existing.remove();

    const providerDisplayName = (pid) => {
        const configured = globalConfig.providers?.[pid]?.name;
        if (configured && configured !== pid) return configured;
        if (_providerNameCache[pid]) return _providerNameCache[pid];
        for (const group of Object.values(KNOWN_PROVIDERS || {})) {
            const found = (group || []).find(p => p.id === pid);
            if (found?.name) {
                _providerNameCache[pid] = found.name;
                return found.name;
            }
        }
        return String(pid || '').replace(/[_-]+/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    };

    const options = [];
    (globalConfig.combos || []).forEach(combo => {
        if (!combo?.alias) return;
        const chain = (combo.chain || []).map(entry => {
            if (typeof entry === 'string') return entry;
            if (entry?.provider && entry?.model) return `${providerDisplayName(entry.provider)} / ${entry.model}`;
            return entry?.model || entry?.id || '';
        }).filter(Boolean).join(' → ');
        options.push({
            group: 'Combo aliases',
            label: combo.alias,
            meta: chain || combo.strategy || 'fallback',
            value: combo.alias,
            kind: 'combo'
        });
    });

    // BSL Models (Blacksand Labs) — listed right under combos. Values are BARE
    // model ids so the backend dispatcher resolves them internally.
    _bslSelectableModels().forEach(m => {
        options.push({
            group: 'BSL Models',
            label: m.name,
            meta: m.desc || 'Blacksand Labs',
            value: m.id,
            kind: 'combo'
        });
    });

    for (const [pid, p] of Object.entries(globalConfig.providers || {})) {
        if (_isBlacksandProvider(pid)) continue; // surfaced above under BSL Models
        if (!isProviderSelectable(p)) continue;
        if (!p.models || p.models.length === 0) continue;
        const pName = providerDisplayName(pid);
        p.models.forEach(m => {
            if (m && m.enabled === false) return;
            options.push({
                group: pName,
                label: `${m.name || m.id}`,
                meta: `${pid}/${m.id}`,
                value: `${pid}/${m.id}`,
                kind: 'model'
            });
        });
    }

    const escapeHtml = (value) => String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

    // Group options by their group name so the layout mirrors the Combo "Add Model" dialog.
    const comboIcon = '<svg viewBox="0 0 24 24" width="16" height="16" stroke="var(--brand-color)" stroke-width="2" fill="none"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>';
    const providerIcon = '<svg viewBox="0 0 24 24" width="16" height="16" stroke="var(--brand-color)" stroke-width="2" fill="none"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>';
    const grouped = [];
    const groupIndex = {};
    options.forEach(o => {
        if (!(o.group in groupIndex)) {
            groupIndex[o.group] = grouped.length;
            grouped.push({ name: o.group, kind: o.kind, items: [] });
        }
        grouped[groupIndex[o.group]].items.push(o);
    });

    const rows = options.length > 0
        ? grouped.map(g => `
            <div class="model-select-group">
                <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px;">
                    ${g.kind === 'combo' ? comboIcon : providerIcon}
                    <span style="font-size: 13px; font-weight: 600; color: var(--brand-color);">${escapeHtml(g.name)} <span style="color: var(--text-muted); font-size: 11px; font-weight: normal;">(${g.items.length})</span></span>
                </div>
                <div style="display: flex; flex-direction: column; gap: 4px;">
                    ${g.items.map(o => `
                        <div class="model-select-row" style="padding:9px 12px;border-radius:8px;border:1px solid var(--border-color);background:var(--bg-body);color:var(--text-main);font-size:13px;font-weight:600;cursor:pointer;user-select:none;transition:all 0.15s;" onclick='selectModelAlias(${JSON.stringify(aliasKey)}, ${JSON.stringify(o.value)})'>
                            ${escapeHtml(o.label)}
                        </div>
                    `).join('')}
                </div>
            </div>
        `).join('')
        : '<div style="text-align: center; color: var(--text-muted); font-size: 13px; padding: 20px 0;">No models configured yet. Add providers and fetch their models first.</div>';

    const overlay = document.createElement('div');
    overlay.id = 'model-select-overlay';
    overlay.style.cssText = 'position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 1010; display: flex; align-items: center; justify-content: center;';
    overlay.innerHTML = `
        <div style="background: var(--bg-surface); width: 480px; border-radius: 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.1); overflow: hidden; display: flex; flex-direction: column; max-height: 80vh;">
            <div style="display: flex; justify-content: space-between; align-items: center; padding: 16px 24px; border-bottom: 1px solid var(--border-color);">
                <div style="display: flex; gap: 6px; align-items: center;">
                    <div style="width: 12px; height: 12px; border-radius: 50%; background: #ff5f56;"></div>
                    <div style="width: 12px; height: 12px; border-radius: 50%; background: #ffbd2e;"></div>
                    <div style="width: 12px; height: 12px; border-radius: 50%; background: #27c93f;"></div>
                    <span style="margin-left: 12px; font-size: 14px; font-weight: 600; color: var(--text-main);">Select Model Mapping</span>
                </div>
                <button class="btn" style="background: none; border: none; padding: 4px; color: var(--text-muted); cursor: pointer;" onclick="document.getElementById('model-select-overlay').remove()">
                    <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                </button>
            </div>
            <div style="padding: 16px 24px; border-bottom: 1px solid var(--border-color);">
                <div style="background: #fff5f5; border: 1px solid #fee2e2; border-radius: 8px; padding: 12px; display: flex; align-items: flex-start; gap: 8px; margin-bottom: 16px;">
                    <svg viewBox="0 0 24 24" width="16" height="16" stroke="var(--brand-color)" stroke-width="2" fill="none" style="margin-top: 2px;"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>
                    <span style="font-size: 12px; color: var(--text-main); line-height: 1.5;">Click a combo alias or provider model to map this slot.</span>
                </div>
                <div style="position: relative;">
                    <svg viewBox="0 0 24 24" width="16" height="16" stroke="var(--text-muted)" stroke-width="2" fill="none" style="position: absolute; left: 12px; top: 50%; transform: translateY(-50%);"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
                    <input type="text" id="model-select-search" placeholder="Search..." style="width: 100%; padding: 10px 12px 10px 36px; border: 1px solid var(--border-color); border-radius: 8px; font-size: 13px; outline: none; background: var(--bg-body); color: var(--text-main);" oninput="filterModelSelect(this.value)">
                </div>
            </div>
            <div id="model-select-list" style="padding: 16px 24px; overflow-y: auto; display: flex; flex-direction: column; gap: 20px;">${rows}</div>
        </div>`;
    overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
    document.body.appendChild(overlay);
    document.getElementById('model-select-search').focus();
};

window.filterModelSelect = (q) => {
    const query = String(q || '').toLowerCase();
    const groups = document.querySelectorAll('#model-select-list .model-select-group');
    if (groups.length === 0) {
        // Fallback for a flat list (no groups rendered).
        document.querySelectorAll('.model-select-row').forEach(row => {
            row.style.display = row.textContent.toLowerCase().includes(query) ? '' : 'none';
        });
        return;
    }
    groups.forEach(group => {
        const header = group.querySelector('span');
        const groupMatch = header ? header.textContent.toLowerCase().includes(query) : false;
        let anyVisible = false;
        group.querySelectorAll('.model-select-row').forEach(row => {
            const match = groupMatch || row.textContent.toLowerCase().includes(query);
            row.style.display = match ? '' : 'none';
            if (match) anyVisible = true;
        });
        group.style.display = anyVisible ? '' : 'none';
    });
};

window.selectModelAlias = (aliasKey, value) => {
    updateAlias(aliasKey, value);
    document.getElementById('model-select-overlay')?.remove();
};

window.generateAPIKey = () => {
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    let rand = '';
    for (let i = 0; i < 32; i++) rand += chars.charAt(Math.floor(Math.random() * chars.length));
    const newKey = 'sk-bsl-' + rand;
    
    if (!globalConfig.keys) globalConfig.keys = [];
    globalConfig.keys.push(newKey);
    saveConfig();
    renderActiveTab();
};

window.deleteAPIKey = (idx) => {
    if (globalConfig.keys) {
        globalConfig.keys.splice(idx, 1);
        saveConfig();
        renderActiveTab();
    }
};

function getModelsDropdownHTML(selectedValue) {
    let html = '';
    const providers = globalConfig.providers || {};
    // Sort provider groups by display name for a stable, scannable list.
    const entries = Object.keys(providers).map(pKey => ({
        pKey,
        p: providers[pKey],
        label: providers[pKey].name || pKey,
    })).sort((a, b) => a.label.localeCompare(b.label));

    for (const { pKey, p, label } of entries) {
        if (!isProviderSelectable(p)) continue;
        if (!p.models || p.models.length === 0) continue;
        // Utility selectors (Docs/Vision/Compaction) should expose EVERY model,
        // grouped by provider — independent of the per-model enabled toggle.
        html += `<optgroup label="${label}">`;
        p.models.forEach(m => {
            const selected = (m.id === selectedValue) ? 'selected' : '';
            html += `<option value="${m.id}" ${selected}>${m.name || m.id}</option>`;
        });
        html += `</optgroup>`;
    }
    return html;
}

// Combo-aware variant for the Tools page utility selectors. Combo aliases sit
// in a leading "Combo Models" optgroup (routed verbatim by the backend), with
// provider models grouped below. Provider models whose id collides with a
// combo alias are de-duped in favor of the combo entry.
function getModelsDropdownWithCombosHTML(selectedValue) {
    let html = '';
    const combos = Array.isArray(globalConfig.combos) ? globalConfig.combos : [];
    const comboAliases = new Set();
    if (combos.length > 0) {
        html += `<optgroup label="Combo Models">`;
        const seenAliases = new Set();
        combos.forEach(c => {
            if (!c || !c.alias || seenAliases.has(c.alias)) return;
            seenAliases.add(c.alias);
            comboAliases.add(c.alias);
            const selected = (c.alias === selectedValue) ? 'selected' : '';
            html += `<option value="${c.alias}" ${selected}>${c.alias}</option>`;
        });
        html += `</optgroup>`;
    }

    // BSL Models (Blacksand Labs) sit directly beneath the Combo Models group.
    html += _bslModelsOptgroupHTML(selectedValue);

    const providers = globalConfig.providers || {};
    const entries = Object.keys(providers).map(pKey => ({
        pKey,
        p: providers[pKey],
        label: providers[pKey].name || pKey,
    })).sort((a, b) => a.label.localeCompare(b.label));

    for (const { pKey, p, label } of entries) {
        if (_isBlacksandProvider(pKey)) continue; // surfaced above under BSL Models
        if (!isProviderSelectable(p)) continue;
        if (!p.models || p.models.length === 0) continue;
        // Collect surviving models first so we don't emit an empty optgroup when
        // every model collided with a combo alias.
        const surviving = p.models.filter(m => !comboAliases.has(m.id));
        if (surviving.length === 0) continue;
        html += `<optgroup label="${label}">`;
        surviving.forEach(m => {
            const id = m.id;
            const selected = (id === selectedValue) ? 'selected' : '';
            html += `<option value="${id}" ${selected}>${m.name || m.id}</option>`;
        });
        html += `</optgroup>`;
    }
    return html;
}

function renderActiveTab() {
    stopLogsLivePolling();  // halt live log streaming when navigating; re-armed on the Logs tab
    let activeNavItem = document.querySelector('.nav-item.active');
    if (!activeNavItem) {
        // Nothing active yet (startup) — activate the first nav item
        activeNavItem = document.querySelector('.nav-item');
        if (activeNavItem) activeNavItem.classList.add('active');
    }
    if (!activeNavItem) return; // DOM not ready yet
    const activeTab = activeNavItem.dataset.tab;
    const content = document.getElementById('main-content');
    
    const visibilityEditBtn = document.getElementById('visibility-edit-btn');
    const visibilitySaveBtn = document.getElementById('visibility-save-btn');
    const saveBtn = document.getElementById('save-btn');

    stopMitmStatusPolling();
    stopAntigravityIntegrationPolling();
    if (activeTab === 'providers') {
        if (currentView === 'list') {
            document.getElementById('topbar-breadcrumb').innerHTML = `
                <svg viewBox="0 0 24 24" width="20" height="20" stroke="var(--brand-color)" stroke-width="2" fill="none" style="margin-right:8px;flex-shrink:0"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="9" y1="21" x2="9" y2="9"/></svg>
                <span>Providers</span>
                <div class="topbar-subtitle">Manage your AI provider connections</div>`;
            content.innerHTML = renderProviderList();
            
            // In editing mode: Show/Hide becomes Cancel; Save Visibility appears; main Save hides
            if (visibilityEditBtn) {
                visibilityEditBtn.style.display = 'inline-flex';
                visibilityEditBtn.textContent = isEditingVisibility ? 'Cancel' : 'Show/Hide Providers';
                visibilityEditBtn.onclick = isEditingVisibility
                    ? () => { isEditingVisibility = false; renderActiveTab(); }
                    : toggleVisibilityMode;
            }
            if (visibilitySaveBtn) visibilitySaveBtn.style.display = isEditingVisibility ? 'inline-flex' : 'none';
            if (saveBtn) saveBtn.style.display = isEditingVisibility ? 'none' : 'inline-flex';
        } else {
            const icon = SVGS[activeProviderId] || letterIcon(activeProviderId);
            document.getElementById('topbar-breadcrumb').innerHTML = `
                <div style="display:flex; align-items:center;">
                    <button class="btn btn-outline" style="padding:4px 8px; font-size:12px; margin-right:12px;" onclick="backToList()">
                        <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><polyline points="15 18 9 12 15 6"></polyline></svg>
                        Back
                    </button>
                    <span class="breadcrumb-back" onclick="backToList()">Providers</span>
                    <span style="color:var(--text-muted);margin:0 6px;">›</span>
                    <span style="display:flex;align-items:center;gap:6px;">${icon} ${getDisplayName(activeProviderId)}</span>
                </div>`;
            content.innerHTML = renderProviderDetail();
            
            if (visibilityEditBtn) visibilityEditBtn.style.display = 'none';
            if (visibilitySaveBtn) visibilitySaveBtn.style.display = 'none';
            if (saveBtn) saveBtn.style.display = 'inline-flex';
        }
    } else if (activeTab === 'endpoint') {
        document.getElementById('topbar-breadcrumb').innerHTML = `
            <svg viewBox="0 0 24 24" width="20" height="20" stroke="var(--brand-color)" stroke-width="2" fill="none" style="margin-right:8px;flex-shrink:0"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/></svg>
            <span>Endpoint & Key</span>
            <div class="topbar-subtitle">Local proxy endpoints and sharing settings</div>`;
        content.innerHTML = renderEndpointTab();
        
        if (visibilityEditBtn) visibilityEditBtn.style.display = 'none';
        if (visibilitySaveBtn) visibilitySaveBtn.style.display = 'none';
        if (saveBtn) saveBtn.style.display = 'inline-flex';
    } else if (activeTab === 'mitm') {
        document.getElementById('topbar-breadcrumb').innerHTML = `
            <svg viewBox="0 0 24 24" width="20" height="20" stroke="var(--brand-color)" stroke-width="2" fill="none" style="margin-right:8px;flex-shrink:0"><path d="M12 2C12 7.52 16.48 12 22 12C16.48 12 12 16.48 12 22C12 16.48 7.52 12 2 12C7.52 12 12 7.52 12 2Z"/></svg>
            <span>Antigravity Integration</span>
            <div class="topbar-subtitle">Direct BSL inference routing with native Google fallback</div>`;
        content.innerHTML = renderMitmTab();
        startAntigravityIntegrationPolling();
        
        if (visibilityEditBtn) visibilityEditBtn.style.display = 'none';
        if (visibilitySaveBtn) visibilitySaveBtn.style.display = 'none';
        if (saveBtn) saveBtn.style.display = 'inline-flex';
    } else if (activeTab === 'tools') {
        document.getElementById('topbar-breadcrumb').innerHTML = `
            <svg viewBox="0 0 24 24" width="20" height="20" stroke="var(--brand-color)" stroke-width="2" fill="none" style="margin-right:8px;flex-shrink:0"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>
            <span>Tools & Intelligence</span>
            <div class="topbar-subtitle">Configure Document Parsing, Vision Bridge, and Context Policies</div>`;
        content.innerHTML = renderToolsTab();
        
        if (visibilityEditBtn) visibilityEditBtn.style.display = 'none';
        if (visibilitySaveBtn) visibilitySaveBtn.style.display = 'none';
        if (saveBtn) saveBtn.style.display = 'inline-flex';
    } else if (activeTab === 'usage') {
        document.getElementById('topbar-breadcrumb').innerHTML = `
            <svg viewBox="0 0 24 24" width="20" height="20" stroke="var(--brand-color)" stroke-width="2" fill="none" style="margin-right:8px;flex-shrink:0"><path d="M21.21 15.89A10 10 0 1 1 8 2.83M22 12A10 10 0 0 0 12 2v10z"/></svg>
            <span>Usage & Costs</span>
            <div class="topbar-subtitle">Token economics and cache savings</div>`;
        content.innerHTML = `<div id="usage-container">Loading...</div>`;
        loadUsageData();
        
        if (visibilityEditBtn) visibilityEditBtn.style.display = 'none';
        if (visibilitySaveBtn) visibilitySaveBtn.style.display = 'none';
        if (saveBtn) saveBtn.style.display = 'none';
    } else if (activeTab === 'logs') {
        document.getElementById('topbar-breadcrumb').innerHTML = `
            <svg viewBox="0 0 24 24" width="20" height="20" stroke="var(--brand-color)" stroke-width="2" fill="none" style="margin-right:8px;flex-shrink:0"><path d="M4 22h14a2 2 0 0 0 2-2V7.5L14.5 2H6a2 2 0 0 0-2 2v4"/><polyline points="14 2 14 8 20 8"/><path d="M2 15h10"/><path d="M2 19h10"/></svg>
            <span>Console & Error Logs</span>
            <div class="topbar-subtitle">Real-time traffic and AI-driven error analysis</div>`;
        content.innerHTML = `<div id="logs-container">Loading...</div>`;
        loadLogsData();
        startLogsLivePolling();
        
        if (visibilityEditBtn) visibilityEditBtn.style.display = 'none';
        if (visibilitySaveBtn) visibilitySaveBtn.style.display = 'none';
        if (saveBtn) saveBtn.style.display = 'none';
    } else if (activeTab === 'combos') {
        document.getElementById('topbar-breadcrumb').innerHTML = `
            <svg viewBox="0 0 24 24" width="20" height="20" stroke="var(--brand-color)" stroke-width="2" fill="none" style="margin-right:8px;flex-shrink:0"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>
            <span>Combo Models</span>
            <div class="topbar-subtitle">Fallback chains — route through multiple models automatically</div>`;
        content.innerHTML = renderCombosTab();

        if (visibilityEditBtn) visibilityEditBtn.style.display = 'none';
        if (visibilitySaveBtn) visibilitySaveBtn.style.display = 'none';
        if (saveBtn) saveBtn.style.display = 'inline-flex';
    } else if (activeTab === 'bsl-models') {
        document.getElementById('topbar-breadcrumb').innerHTML = `
            <svg viewBox="0 0 24 24" width="20" height="20" stroke="var(--brand-color)" stroke-width="2" fill="none" style="margin-right:8px;flex-shrink:0"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
            <span>BSL Models</span>
            <div class="topbar-subtitle">Model family routing matrix — category × complexity → combo</div>`;
        content.innerHTML = renderBslModelsTab();

        if (visibilityEditBtn) visibilityEditBtn.style.display = 'none';
        if (visibilitySaveBtn) visibilitySaveBtn.style.display = 'none';
        if (saveBtn) saveBtn.style.display = 'inline-flex';
    } else if (activeTab === 'settings') {
        document.getElementById('topbar-breadcrumb').innerHTML = `
            <svg viewBox="0 0 24 24" width="20" height="20" stroke="var(--brand-color)" stroke-width="2" fill="none" style="margin-right:8px;flex-shrink:0"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
            <span>Settings</span>
            <div class="topbar-subtitle">Admin security and system controls</div>`;
        content.innerHTML = renderSettingsTab();
        startAfzPolling(); // begin live stream-count polling

        if (visibilityEditBtn) visibilityEditBtn.style.display = 'none';
        if (visibilitySaveBtn) visibilitySaveBtn.style.display = 'none';
        if (saveBtn) saveBtn.style.display = 'none';
    } else {
        stopAfzPolling(); // stop polling when leaving settings tab
        if (visibilityEditBtn) visibilityEditBtn.style.display = 'none';
        if (visibilitySaveBtn) visibilitySaveBtn.style.display = 'none';
        if (saveBtn) saveBtn.style.display = 'inline-flex';
    }
}

// ─── BSL Models Tab — Matrix Grid (v2: 3-slot cells + dual route sources) ──────
// Renders the 13-category × 3-tier (fast/standard/deep) routing matrix for
// bsl-chat. Each cell has 3 slots: primary, fallback_1, fallback_2.
// Each slot accepts EITHER a combo alias (e.g. coder-2) OR a provider-qualified
// single model (e.g. vietapi-a/opus-4.6).
//
// Config schema (in-memory, hardened v3 — matches bsl_chat_router.py 4-step precedence):
//   bsl_models.bsl_chat = {
//     enabled: bool,                                  // master switch
//     default_route_enabled: bool,                    // Step 0: bypass entire matrix
//     default_route: "coder-2" | {primary,fallback_1,fallback_2},  // Step 0 model
//     global_last_fallback: "coder-1" | {...},        // Step 3 safety net (always active)
//     auto_selected_slots: {                          // Provenance metadata for Clear Auto-Selection
//       "technical/fast/primary": true, ...
//     },
//     category_overrides: {
//       technical: {
//         fast:     { primary: "...", fallback_1: "...", fallback_2: "..." },
//         standard: { ... },
//         deep:     { ... }
//       },
//       general: { ... },  // Step 2: general fallback category
//       ...
//     }
//   }
//
// Backend precedence (bsl_chat_router.py _select_model):
//   0. default_route (override) → bypasses entire matrix
//   1. category_overrides[classified_category][bucket]
//   2. category_overrides["general"][bucket]  (general fallback)
//   3. global_last_fallback (always attempted when configured — no toggle)
//
// The 12 scorable categories + 1 fallback (general) = 13 total, matching
// the locked taxonomy in category_classifier.py.

const BSL_CATEGORIES = [
    { id: 'technical',  icon: '⚙️', label: 'Technical' },
    { id: 'law',        icon: '⚖️', label: 'Law' },
    { id: 'health',     icon: '🏥', label: 'Health' },
    { id: 'finance',    icon: '💰', label: 'Finance' },
    { id: 'business',   icon: '📊', label: 'Business' },
    { id: 'geopolitics', icon: '🌐', label: 'Geopolitics' },
    { id: 'creative',   icon: '🎨', label: 'Creative' },
    { id: 'education',  icon: '📚', label: 'Education' },
    { id: 'research',   icon: '🔬', label: 'Research' },
    { id: 'science',    icon: '🧪', label: 'Science' },
    { id: 'lifestyle',  icon: '🌿', label: 'Lifestyle' },
    { id: 'philosophy', icon: '🤔', label: 'Philosophy' },
    { id: 'general',    icon: '◯', label: 'General (Fallback)' }
];

// BSL-Lite uses OAC (Open Atelier Code) agent taxonomy.
// BSL-Lite targets coding agents/IDEs (Claude Code, Cursor, Aider), NOT general chat.
// 8 OAC agents (General is merged into Scout — no separate general fallback).
// BSL-Lite rule: one role at a time. Multiple calls OK within role boundary.
// Followup suggestions allowed but never auto-execute — wait for user approval.
const BSL_LITE_CATEGORIES = [
    { id: 'scout',          icon: '🔭', label: 'Scout' },
    { id: 'fast_coder',     icon: '⚡', label: 'FastCoder' },
    { id: 'power_coder',    icon: '🔧', label: 'PowerCoder' },
    { id: 'ultra_coder',    icon: '🚀', label: 'UltraCoder' },
    { id: 'refactor',       icon: '♻️', label: 'Refactor' },
    { id: 'frontend_coder', icon: '🎨', label: 'FrontendCoder' }
];

// ── BSL-Agentic (Fast) Agent Taxonomy ──
// 11 rows: 7 backend agents + 4 specialist rows (formerly sub-agents) + vision.
// Sub-agents share their parent's route config (single key in agent_routes).
// Vision is UI-only placeholder — backend classifier has no vision category yet.
const BSL_AGENTIC_CATEGORIES = [
    { id: 'vision',              icon: '👁️', label: 'Vision',              parent: null,    desc: 'Document/image analysis — runs first on attachments' },
    { id: 'scout',               icon: '🔭', label: 'Scout',               parent: null,    desc: 'Search/read — internal (codebase) + external (web)' },
    { id: 'planner_architect',   icon: '🏛️', label: 'Planner.Architect',   parent: 'planner', desc: 'Architecture design' },
    { id: 'planner_challenger',  icon: '⚔️', label: 'Planner.Challenger',  parent: 'planner', desc: 'Architect challenge' },
    { id: 'planner_planner',     icon: '📋', label: 'Planner.Planner',     parent: 'planner', desc: 'Task decomposition' },
    { id: 'auditor_reviewer',    icon: '📖', label: 'Auditor.Reviewer',    parent: 'auditor', desc: 'Plan review/challenge' },
    { id: 'auditor_auditor',     icon: '🔬', label: 'Auditor.Auditor',     parent: 'auditor', desc: 'Code/fix audit' },
    { id: 'refactor',            icon: '♻️', label: 'Refactor',            parent: null,    desc: 'Code restructuring' },
    { id: 'fast_coder',          icon: '⚡', label: 'FastCoder',           parent: null,    desc: 'Quick implementation' },
    { id: 'power_coder',         icon: '🔧', label: 'PowerCoder',          parent: null,    desc: 'Standard implementation' },
    { id: 'ultra_coder',         icon: '🚀', label: 'UltraCoder',          parent: null,    desc: 'Complex implementation' },
    { id: 'frontend_coder',      icon: '🎨', label: 'FrontendCoder',       parent: null,    desc: 'UI/UX implementation' },
];

// ── BSL-Agentic Ultra (Balanced) Agent Taxonomy ──
// Same 13 rows as Agentic + 4 additional member/challenger sub-agent rows.
// Routing is single-agent: Scout classifies, then that agent's own
// Primary -> Fallback 1 -> Fallback 2 chain resolves. There is no consult stage.
const BSL_AGENTIC_ULTRA_CATEGORIES = [
    { id: 'vision',                      icon: '👁️', label: 'Vision',                      parent: null,      desc: 'Document/image analysis — runs first on attachments' },
    { id: 'scout',                       icon: '🔭', label: 'Scout',                       parent: null,      desc: 'Search/read — internal (codebase) + external (web)' },
    { id: 'planner_architect',           icon: '🏛️', label: 'Planner.Architect',           parent: 'planner',  desc: 'Architecture design' },
    { id: 'planner_challenger',          icon: '⚔️', label: 'Planner.Challenger',          parent: 'planner',  desc: 'Architect challenge' },
    { id: 'planner_challenger_member',   icon: '⚔️+', label: 'Planner.Challenger Member',  parent: 'planner',  desc: 'Challenger subagent' },
    { id: 'planner_planner',             icon: '📋', label: 'Planner.Planner',             parent: 'planner',  desc: 'Task decomposition' },
    { id: 'auditor_reviewer',            icon: '📖', label: 'Auditor.Reviewer',            parent: 'auditor',  desc: 'Plan review/challenge' },
    { id: 'auditor_reviewer_member',     icon: '📖+', label: 'Auditor.Reviewer Member',    parent: 'auditor',  desc: 'Reviewer subagent' },
    { id: 'auditor_auditor',             icon: '🔬', label: 'Auditor.Auditor',             parent: 'auditor',  desc: 'Code/fix audit' },
    { id: 'auditor_auditor_member',      icon: '🔬+', label: 'Auditor.Auditor Member',     parent: 'auditor',  desc: 'Auditor subagent' },
    { id: 'refactor',                    icon: '♻️', label: 'Refactor',                    parent: null,      desc: 'Code restructuring' },
    { id: 'fast_coder',                  icon: '⚡', label: 'FastCoder',                   parent: null,      desc: 'Quick implementation' },
    { id: 'power_coder',                 icon: '🔧', label: 'PowerCoder',                  parent: null,      desc: 'Standard implementation' },
    { id: 'ultra_coder',                 icon: '🚀', label: 'UltraCoder',                  parent: null,      desc: 'Complex implementation' },
    { id: 'frontend_coder',              icon: '🎨', label: 'FrontendCoder',               parent: null,      desc: 'UI/UX implementation' },
];

const BSL_BUCKETS = [
    { id: 'fast',     label: 'Fast',     tier: 'bsl-tier-fast',     hint: 'Trivial/Simple' },
    { id: 'standard', label: 'Standard', tier: 'bsl-tier-standard', hint: 'Standard' },
    { id: 'deep',     label: 'Deep',     tier: 'bsl-tier-deep',     hint: 'Complex' }
];

const BSL_SLOTS = [
    { id: 'primary',   label: 'P',  badgeClass: 'primary',  title: 'Primary route' },
    { id: 'fallback_1', label: 'F1', badgeClass: 'fallback', title: 'Fallback 1' },
    { id: 'fallback_2', label: 'F2', badgeClass: 'f2',       title: 'Fallback 2' }
];

// Product UI uses canonical public IDs; config storage remains bsl_models.*.
const BSL_FAMILIES = [
    { id: 'blacksand-chat',          label: 'Blacksand Chat',          status: 'active',  desc: 'Category-aware smart routing' },
    { id: 'blacksand-lite',          label: 'Blacksand Lite',          status: 'active',  desc: 'Coding-agent single-task router (8-agent matrix)' },
    { id: 'blacksand-agentic',       label: 'Blacksand Agentic',       status: 'active',  desc: 'Fast-tier agentic coding orchestration' },
    { id: 'blacksand-agentic-ultra', label: 'Blacksand Agentic Ultra', status: 'active',  desc: 'Balanced-tier coding orchestration' },
    { id: 'blacksand-agentic-max',   label: 'Blacksand Agentic Max',   status: 'active',  desc: 'Multi-domain fusion orchestration' }
];

function _getBslChatCfg() {
    // Canonical: bsl_models.bsl_chat — Legacy: bsl_chat
    if (!globalConfig.bsl_models) globalConfig.bsl_models = {};
    if (!globalConfig.bsl_models.bsl_chat) {
        // Migrate legacy or create fresh
        if (globalConfig.bsl_chat && typeof globalConfig.bsl_chat === 'object') {
            globalConfig.bsl_models.bsl_chat = globalConfig.bsl_chat;
            delete globalConfig.bsl_chat;
        } else {
            globalConfig.bsl_models.bsl_chat = {};
        }
    }
    const cfg = globalConfig.bsl_models.bsl_chat;
    // Defaults — aligned with bsl_chat_router.py 4-step precedence
    if (cfg.enabled === undefined) cfg.enabled = false;
    if (!cfg.category_overrides) cfg.category_overrides = {};
    if (!cfg.auto_selected_slots) cfg.auto_selected_slots = {};

    // ── Legacy v1→v2 migration: convert bare string values to 3-slot dicts ──
    // Old: category_overrides.technical.fast = "coder-1"
    // New: category_overrides.technical.fast = { primary: "coder-1" }
    for (const cat of Object.keys(cfg.category_overrides)) {
        const catObj = cfg.category_overrides[cat];
        if (!catObj || typeof catObj !== 'object') continue;
        for (const bucket of Object.keys(catObj)) {
            const val = catObj[bucket];
            if (typeof val === 'string') {
                catObj[bucket] = { primary: val };
            }
        }
    }

    // ── Dead key cleanup: purge default_combo_by_complexity if it exists ──
    // This key was written by old UI code but NEVER read by the backend.
    // Migrate its contents to category_overrides.general (step 2 in precedence).
    if (cfg.default_combo_by_complexity) {
        if (!cfg.category_overrides.general) cfg.category_overrides.general = {};
        for (const bucket of Object.keys(cfg.default_combo_by_complexity)) {
            const val = cfg.default_combo_by_complexity[bucket];
            if (!cfg.category_overrides.general[bucket]) {
                cfg.category_overrides.general[bucket] = (typeof val === 'string') ? { primary: val } : val;
            }
        }
        delete cfg.default_combo_by_complexity;
    }

    // ── Purge obsolete global_last_fallback_enabled toggle ──
    // Global Last Fallback is always active when configured; the toggle is
    // removed from the UI and must not persist as stale state that could
    // (in any future reader) be mistaken for a disable switch.
    delete cfg.global_last_fallback_enabled;

    return cfg;
}

// ─── Blacksand Lite Config ─────────────────────────────────────────────────
// BSL-Lite is the non-agentic single-task router (mirrors OAC's 'direct' mode).
// It has the SAME full 13×3 matrix as BSL-Chat, with its own independent model
// selections per cell. "Lite" = non-agentic (no multi-step orchestration), NOT
// single-model.
function _getBslLiteCfg() {
    if (!globalConfig.bsl_models) globalConfig.bsl_models = {};
    if (!globalConfig.bsl_models.bsl_lite) {
        // Migrate legacy top-level bsl_lite or create fresh
        if (globalConfig.bsl_lite && typeof globalConfig.bsl_lite === 'object') {
            globalConfig.bsl_models.bsl_lite = globalConfig.bsl_lite;
            delete globalConfig.bsl_lite;
        } else {
            globalConfig.bsl_models.bsl_lite = {};
        }
    }
    const cfg = globalConfig.bsl_models.bsl_lite;
    // Defaults — aligned with bsl_lite_router.py 4-step precedence
    if (cfg.enabled === undefined) cfg.enabled = false;
    if (!cfg.category_overrides) cfg.category_overrides = {};
    if (!cfg.auto_selected_slots) cfg.auto_selected_slots = {};

    // ── Legacy single-route → flat migration ──
    // Old: bsl_lite.route = "coder-2" (single model, no matrix)
    // New: bsl_lite.category_overrides.scout.primary = "coder-2"
    // Preserve the existing route as the scout primary slot.
    if (cfg.route) {
        if (!cfg.category_overrides.scout || typeof cfg.category_overrides.scout !== 'object') {
            cfg.category_overrides.scout = {};
        }
        if (!cfg.category_overrides.scout.primary) {
            cfg.category_overrides.scout.primary = cfg.route;
        }
        delete cfg.route;
    }

    // ── Legacy tiered → flat migration ──
    // Old: category_overrides[cat] = { fast: {...}, standard: {...}, deep: {...} }
    // New: category_overrides[cat] = { primary, fallback_1, fallback_2 }
    // Migrate by taking the standard tier's slots as the flat route.
    for (const cat of Object.keys(cfg.category_overrides)) {
        const catObj = cfg.category_overrides[cat];
        if (!catObj) { delete cfg.category_overrides[cat]; continue; }
        if (typeof catObj === 'string') {
            // Bare string → 3-slot dict with primary
            cfg.category_overrides[cat] = { primary: catObj };
            continue;
        }
        if (typeof catObj !== 'object') { delete cfg.category_overrides[cat]; continue; }
        const hasTiers = ['fast', 'standard', 'deep'].some(t => t in catObj);
        if (hasTiers) {
            const std = catObj.standard || catObj.fast || catObj.deep || {};
            const flat = {};
            for (const slot of ['primary', 'fallback_1', 'fallback_2']) {
                if (std[slot]) flat[slot] = std[slot];
            }
            if (Object.keys(flat).length > 0) {
                cfg.category_overrides[cat] = flat;
            } else {
                delete cfg.category_overrides[cat];
            }
        }
    }

    // ── Purge obsolete global_last_fallback_enabled toggle ──
    delete cfg.global_last_fallback_enabled;

    return cfg;
}

// ─── BSL Agentic Config ────────────────────────────────────────────────────
function _getBslAgenticCfg() {
    if (!globalConfig.bsl_models) globalConfig.bsl_models = {};
    if (!globalConfig.bsl_models.bsl_agentic) {
        if (globalConfig.bsl_agentic && typeof globalConfig.bsl_agentic === 'object') {
            globalConfig.bsl_models.bsl_agentic = globalConfig.bsl_agentic;
            delete globalConfig.bsl_agentic;
        } else {
            globalConfig.bsl_models.bsl_agentic = {};
        }
    }
    const cfg = globalConfig.bsl_models.bsl_agentic;
    if (cfg.enabled === undefined) cfg.enabled = false;
    if (!cfg.agent_routes) cfg.agent_routes = {};
    if (!cfg.global_last_fallback) cfg.global_last_fallback = '';
    return cfg;
}

// ─── BSL Agentic Ultra Config ──────────────────────────────────────────────
function _getBslAgenticUltraCfg() {
    if (!globalConfig.bsl_models) globalConfig.bsl_models = {};
    if (!globalConfig.bsl_models.bsl_agentic_ultra) {
        if (globalConfig.bsl_agentic_ultra && typeof globalConfig.bsl_agentic_ultra === 'object') {
            globalConfig.bsl_models.bsl_agentic_ultra = globalConfig.bsl_agentic_ultra;
            delete globalConfig.bsl_agentic_ultra;
        } else {
            globalConfig.bsl_models.bsl_agentic_ultra = {};
        }
    }
    const cfg = globalConfig.bsl_models.bsl_agentic_ultra;
    if (cfg.enabled === undefined) cfg.enabled = false;
    if (!cfg.agent_routes) cfg.agent_routes = {};
    // NOTE: `consult_routes` and `consult_threshold` are deliberately NOT seeded.
    // bsl_agentic_ultra_router ignores both (RouteDecision.consulted is hardcoded
    // False), so seeding them only wrote dead keys into config.yaml.
    if (!cfg.global_last_fallback) cfg.global_last_fallback = '';
    return cfg;
}

// ─── BSL Agentic Max Config ────────────────────────────────────────────────
function _getBslAgenticMaxCfg() {
    if (!globalConfig.bsl_models) globalConfig.bsl_models = {};
    if (!globalConfig.bsl_models.bsl_agentic_max) {
        if (globalConfig.bsl_agentic_max && typeof globalConfig.bsl_agentic_max === 'object') {
            globalConfig.bsl_models.bsl_agentic_max = globalConfig.bsl_agentic_max;
            delete globalConfig.bsl_agentic_max;
        } else {
            globalConfig.bsl_models.bsl_agentic_max = {};
        }
    }
    const cfg = globalConfig.bsl_models.bsl_agentic_max;
    if (cfg.enabled === undefined) cfg.enabled = false;
    if (!cfg.agent_routes) cfg.agent_routes = {};
    if (!cfg.chat_routes) cfg.chat_routes = {};
    if (!cfg.merge_strategy) cfg.merge_strategy = 'confidence_weighted';
    if (!cfg.global_last_fallback) cfg.global_last_fallback = '';
    return cfg;
}

// Build <option> list with TWO optgroups: Combo Models + Provider Models.
// Each provider model is qualified as providerKey/modelId so the backend
// resolver can route it directly.
function _getRouteOptionsHTML(selectedValue) {
    let html = '';

    // ── Combo aliases ──
    const knownAliases = ['coder-1', 'coder-2', 'coder-3'];
    const comboAliases = (globalConfig.combos || []).map(c => c.alias).filter(a => !knownAliases.includes(a));
    const allAliases = [...knownAliases, ...comboAliases];

    html += '<optgroup label="Combo Models">';
    for (const alias of allAliases) {
        html += `<option value="${alias}" ${selectedValue === alias ? 'selected' : ''}>${alias}</option>`;
    }
    html += '</optgroup>';

    // ── Provider models ──
    const providers = globalConfig.providers || {};
    for (const [provKey, prov] of Object.entries(providers)) {
        if (!isProviderSelectable(prov)) continue;
        const models = (prov.models || []).filter(m => m.enabled !== false);
        if (models.length === 0) continue;
        const provName = prov.name || provKey;
        html += `<optgroup label="${provName} (${provKey})">`;
        for (const m of models) {
            const routeId = `${provKey}/${m.id}`;
            const label = m.name || m.id;
            html += `<option value="${routeId}" ${selectedValue === routeId ? 'selected' : ''}>${label}</option>`;
        }
        html += '</optgroup>';
    }

    return html;
}

// Helper: get a slot value from a cell object, returning '' for missing
function _getSlotVal(cellObj, slotId) {
    if (!cellObj || typeof cellObj !== 'object') return '';
    return cellObj[slotId] || '';
}

// Helper: check if a cell has any non-empty slot
function _cellHasValue(cellObj) {
    if (!cellObj || typeof cellObj !== 'object') return false;
    return BSL_SLOTS.some(s => cellObj[s.id]);
}

// Helper: format a cell for display in inherited tooltip
function _cellSummary(cellObj) {
    if (!cellObj || typeof cellObj !== 'object') return '—';
    const parts = BSL_SLOTS.map(s => cellObj[s.id]).filter(v => v);
    return parts.length > 0 ? parts.join(' → ') : '—';
}

let _activeBslFamily = 'blacksand-chat';

window.switchBslFamily = function(familyId) {
    _activeBslFamily = familyId;
    renderActiveTab();
};

function renderBslModelsTab() {
    const cfg = _getBslChatCfg();

    // ── Family pills (top) ──
    let familyPillsHtml = '';
    for (const fam of BSL_FAMILIES) {
        const isActive = fam.id === _activeBslFamily;
        const isDisabled = fam.status !== 'active';
        const pillClass = `bsl-family-pill ${isActive ? 'active' : ''} ${isDisabled ? 'disabled' : ''}`;
        familyPillsHtml += `
            <div class="${pillClass}" ${isDisabled ? '' : `onclick="switchBslFamily('${fam.id}')"`} title="${fam.desc}">
                <div class="status-dot"></div>
                ${fam.label}
                ${fam.status !== 'active' ? '<span class="bsl-coming-soon">Soon</span>' : ''}
            </div>`;
    }

    // ── Render content based on active family ──
    if (_activeBslFamily === 'blacksand-lite') {
        return `
            <div class="bsl-family-pills">
                ${familyPillsHtml}
            </div>
            ${_renderBslLiteConfig()}`;
    }

    if (_activeBslFamily === 'blacksand-agentic') {
        return `
            <div class="bsl-family-pills">
                ${familyPillsHtml}
            </div>
            ${_renderBslAgenticConfig()}`;
    }

    if (_activeBslFamily === 'blacksand-agentic-ultra') {
        return `
            <div class="bsl-family-pills">
                ${familyPillsHtml}
            </div>
            ${_renderBslAgenticUltraConfig()}`;
    }

    if (_activeBslFamily === 'blacksand-agentic-max') {
        return `
            <div class="bsl-family-pills">
                ${familyPillsHtml}
            </div>
            ${_renderBslAgenticMaxConfig()}`;
    }

    // ── General fallback defaults (step 2 in precedence) ──
    // Reads from category_overrides.general — what the backend actually reads.
    const defaultsByBucket = (cfg.category_overrides && cfg.category_overrides.general) || {};
    const globalSectionHtml = `
        <div class="bsl-matrix-card">
            <div class="bsl-matrix-card-header">
                <div>
                    <h2>Blacksand Chat — Global Configuration</h2>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">Default routing for all categories — always-on</div>
                </div>
            </div>

            <div class="bsl-global-row">
                <div class="setting-info">
                    <div class="setting-title">Default Route Override</div>
                    <div class="setting-desc">When enabled, <strong>bypasses the entire category×complexity matrix</strong> — ALL requests route to this one model. Use this to simplify routing when you don't need per-category intelligence.</div>
                </div>
                <div style="display:flex;align-items:center;gap:12px">
                    <label class="switch">
                        <input type="checkbox" onchange="_setBslChatField('default_route_enabled', this.checked); renderActiveTab()" ${cfg.default_route_enabled ? 'checked' : ''}>
                        <span class="slider"></span>
                    </label>
                    <select class="input" style="width:240px" onchange="_setBslChatField('default_route', this.value)" ${cfg.default_route_enabled ? '' : 'disabled'}>
                        <option value="">— none —</option>
                        ${_getRouteOptionsHTML(cfg.default_route)}
                    </select>
                </div>
            </div>

            <div class="bsl-global-row">
                <div class="setting-info">
                    <div class="setting-title">Global Last Fallback</div>
                    <div class="setting-desc">Final safety net appended to every fallback chain. Always active when configured — no on/off toggle. Should be a reliable, always-available model.</div>
                </div>
                <div style="display:flex;align-items:center;gap:12px">
                    <select class="input" style="width:240px" onchange="_setBslChatField('global_last_fallback', this.value)">
                        <option value="">— none —</option>
                        ${_getRouteOptionsHTML(cfg.global_last_fallback)}
                    </select>
                </div>
            </div>
        </div>`;

    // ── 13×3 Matrix grid (3 slots per cell) ──
    const categoryOverrides = cfg.category_overrides || {};
    const matrixRowsHtml = BSL_CATEGORIES.map(cat => {
        const overrides = categoryOverrides[cat.id] || {};
        const cellsHtml = BSL_BUCKETS.map(b => {
            const cell = overrides[b.id] || {};
            const hasOverride = _cellHasValue(cell);
            const slotsHtml = BSL_SLOTS.map(s => {
                const val = _getSlotVal(cell, s.id);
                const markerKey = `${cat.id}/${b.id}/${s.id}`;
                const isAutoMarked = cfg.auto_selected_slots && cfg.auto_selected_slots[markerKey] === true;
                const selectClass = `bsl-slot-select ${val ? (isAutoMarked ? 'auto-marked' : 'has-override') : 'inherited'}`;
                return `
                    <div class="bsl-slot-row">
                        <span class="bsl-slot-badge ${s.badgeClass}" title="${s.title}">${s.label}</span>
                        <select class="${selectClass}" onchange="_setCategorySlot('${cat.id}', '${b.id}', '${s.id}', this.value)" title="${val ? (isAutoMarked ? 'Auto-selected — edit to make manual' : 'Override active') : 'Empty'}">
                            <option value="">— none —</option>
                            ${_getRouteOptionsHTML(val)}
                        </select>
                    </div>`;
            }).join('');
            return `
                <td>
                    <div class="bsl-cell-slots">
                        ${slotsHtml}
                    </div>
                </td>`;
        }).join('');
        return `
            <tr>
                <td>
                    <div class="bsl-cat-cell">
                        <div class="bsl-cat-icon">${cat.icon}</div>
                        ${cat.label}
                    </div>
                </td>
                ${cellsHtml}
            </tr>`;
    }).join('');

    const matrixHtml = `
        <div class="bsl-matrix-card">
            <div class="bsl-matrix-card-header">
                <div>
                    <h2>Category × Complexity Matrix</h2>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">13 categories × 3 tiers × 3 slots (P/F1/F2) — each slot accepts combo or provider model</div>
                </div>
                <div style="display:flex;gap:8px">
                    <button class="btn btn-outline" style="font-size:12px;padding:6px 12px;background:var(--brand-light);color:var(--brand-color);border-color:var(--brand-color);" onclick="_runAutoSelect()" title="Fill empty matrix slots from the predefined benchmark-backed recommendations">⚡ Auto-Select</button>
                    <button class="btn btn-outline" style="font-size:12px;padding:6px 12px;" onclick="_clearAllBslOverrides()">Clear Auto-Selection</button>
                </div>
            </div>
            <table class="bsl-matrix-table">
                <thead>
                    <tr>
                        <th>Category</th>
                        ${BSL_BUCKETS.map(b => `<th class="${b.tier}">${b.label}<br><span style="font-weight:400;text-transform:none;font-size:10px;color:var(--text-muted);">${b.hint}</span></th>`).join('')}
                    </tr>
                </thead>
                <tbody>
                    ${matrixRowsHtml}
                </tbody>
            </table>
        </div>`;

    // ── Routing logic visualization ──
    const flowHtml = `
        <div class="bsl-matrix-card">
            <div class="bsl-matrix-card-header">
                <div>
                    <h2>Resolution Chain</h2>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">How bsl-chat resolves a request to a concrete route</div>
                </div>
            </div>
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:13px;color:var(--text-muted);">
                ${cfg.default_route_enabled
                    ? `<span style="padding:6px 12px;background:#ede9fe;border-radius:8px;color:#7c3aed;font-weight:600;">Default Route</span>
                       <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="9 18 15 12 9 6"/></svg>
                       <span style="padding:6px 12px;background:#ede9fe;border-radius:8px;color:#7c3aed;font-weight:600;">Global Last Fallback</span>`
                    : `<span style="padding:6px 12px;background:var(--brand-light);border-radius:8px;color:var(--brand-color);font-weight:600;">Category P→F1→F2</span>
                       <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="9 18 15 12 9 6"/></svg>
                       <span style="padding:6px 12px;background:#f3f4f6;border-radius:8px;font-weight:500;">General Fallback P→F1→F2</span>
                       <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="9 18 15 12 9 6"/></svg>
                       <span style="padding:6px 12px;background:#ede9fe;border-radius:8px;color:#7c3aed;font-weight:600;">Global Last Fallback</span>`
                }
                <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="9 18 15 12 9 6"/></svg>
                <span style="padding:6px 12px;background:#fee2e2;border-radius:8px;color:var(--danger);font-weight:500;">503 No Model</span>
            </div>
            <div style="font-size:12px;color:var(--text-muted);margin-top:12px;line-height:1.6;">
                ${cfg.default_route_enabled
                    ? '<strong>Default Route ON:</strong> All Blacksand Chat requests bypass the category/complexity matrix and use the single selected route. If it fails, Global Last Fallback is tried, then 503.'
                    : '<strong>Default Route OFF:</strong> Requests flow through Category (P→F1→F2) → General Fallback (P→F1→F2) → Global Last Fallback → 503.'
                }<br>
                <strong>Global Last Fallback:</strong> Always active when configured — no disable toggle.<br>
                <strong>Always on:</strong> Smart routing is the core dispatch path and is <strong>always active</strong>. The blacksand-chat toggle controls catalog visibility only; routing behavior never changes.
            </div>
        </div>`;

    return `
        <div class="bsl-family-pills">
            ${familyPillsHtml}
        </div>
        ${/* ── BSL-Lite config card — shown inside bsl-lite tab ── */''}
        ${globalSectionHtml}
        ${matrixHtml}
        ${flowHtml}`;
}

// ─── Agentic Matrix Renderer (shared helper) ──────────────────────────────
// Renders the agent matrix table for any agentic variant.
// categories: array of {id, icon, label, parent, desc}
// agentRoutes: config.agent_routes object
// editable: whether slots are editable (false for Max read-only view)
// setterName: JS function name for slot changes
// showSubAgentNote: whether to show the "shares parent route" note
function _renderAgenticMatrix(categories, agentRoutes, editable, setterName, showSubAgentNote) {
    const rowsHtml = categories.map(cat => {
        // Sub-agents use their own route when configured; otherwise inherit parent.
        const isSubAgent = !!cat.parent;
        const routeKey = cat.id;
        const agentRoute = agentRoutes[routeKey] || {};
        const isVision = cat.id === 'vision';

        const slotsHtml = BSL_SLOTS.map(s => {
            const val = _getSlotVal(agentRoute, s.id);
            const markerKey = `${routeKey}/${s.id}`;
            const isAutoMarked = agentRoute && agentRoute[s.id] &&
                ((window._activeAgenticAutoMarkers || {})[markerKey] === true);
            if (!editable) {
                return `<td><span style="font-size:12px;color:${val ? 'var(--text-primary)' : 'var(--text-muted)'};font-family:monospace;">${val || '—'}</span></td>`;
            }
            const selectClass = `bsl-slot-select ${val ? (isAutoMarked ? 'auto-marked' : 'has-override') : 'inherited'}`;
            return `
                <td>
                    <select class="${selectClass}" onchange="${setterName}('${routeKey}', '${s.id}', this.value)" title="${val ? (isAutoMarked ? 'Auto-selected' : 'Override active') : 'Empty'}">
                        <option value="">— none —</option>
                        ${_getRouteOptionsHTML(val)}
                    </select>
                </td>`;
        }).join('');

        // Rows are PEERS, not children: since the granular-route fix, every row
        // has its own independently-configurable route. `parent` is retained for
        // backend parent-fallback resolution only (planner_architect -> planner)
        // and must NOT drive indentation, or the UI implies the old
        // "sub-agents share their parent's route" behavior that no longer exists.
        const indentStyle = '';
        const iconStyle = '';
        const visionBadge = isVision ? ' <span style="font-size:10px;background:#fef3c7;color:#92400e;padding:1px 6px;border-radius:4px;font-weight:700;">PRE-FLIGHT</span>' : '';
        const subAgentNote = '';

        return `
            <tr>
                <td>
                    <div class="bsl-cat-cell" style="${indentStyle}">
                        <div class="bsl-cat-icon" style="${iconStyle}">${cat.icon}</div>
                        <div>
                            <div>${cat.label}${visionBadge}${subAgentNote}</div>
                            <div style="font-size:11px;color:var(--text-muted);font-weight:400;">${cat.desc}</div>
                        </div>
                    </div>
                </td>
                ${slotsHtml}
            </tr>`;
    }).join('');

    return `
        <table class="bsl-matrix-table">
            <thead>
                <tr>
                    <th style="width:280px;">Agent</th>
                    <th>Primary</th>
                    <th>Fallback 1</th>
                    <th>Fallback 2</th>
                </tr>
            </thead>
            <tbody>
                ${rowsHtml}
            </tbody>
        </table>`;
}

// ─── BSL Agentic (Fast) Render ────────────────────────────────────────────
function _renderBslAgenticConfig() {
    const cfg = _getBslAgenticCfg();

    const globalSectionHtml = `
        <div class="bsl-matrix-card">
            <div class="bsl-matrix-card-header">
                <div>
                    <h2>Blacksand Agentic — Global Configuration</h2>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">Fast-tier agentic coding orchestration. Depth = fast. 11 agents, single-agent routing, no complexity logic.</div>
                </div>
                <div style="display:flex;align-items:center;gap:12px;">
                    <span style="font-size:11px;font-weight:800;color:${cfg.enabled ? 'var(--success)' : 'var(--text-muted)'};text-transform:uppercase;">${cfg.enabled ? 'Active' : 'Paused'}</span>
                    <label class="switch">
                        <input type="checkbox" onchange="_setBslAgenticEnabled(this.checked)" ${cfg.enabled ? 'checked' : ''}>
                        <span class="slider"></span>
                    </label>
                </div>
            </div>

            <div class="bsl-global-row">
                <div class="setting-info">
                    <div class="setting-title">Default Route Override</div>
                    <div class="setting-desc">When enabled, <strong>bypasses the entire agent matrix</strong> — ALL requests route to this one model.</div>
                </div>
                <div style="display:flex;align-items:center;gap:12px">
                    <label class="switch">
                        <input type="checkbox" onchange="_setBslAgenticField('default_route_enabled', this.checked); renderActiveTab()" ${cfg.default_route_enabled ? 'checked' : ''}>
                        <span class="slider"></span>
                    </label>
                    <select class="input" style="width:240px" onchange="_setBslAgenticField('default_route', this.value)" ${cfg.default_route_enabled ? '' : 'disabled'}>
                        <option value="">— none —</option>
                        ${_getRouteOptionsHTML(cfg.default_route)}
                    </select>
                </div>
            </div>

            <div class="bsl-global-row">
                <div class="setting-info">
                    <div class="setting-title">Global Last Fallback</div>
                    <div class="setting-desc">Final safety net appended to every fallback chain. Always active when configured.</div>
                </div>
                <div style="display:flex;align-items:center;gap:12px">
                    <select class="input" style="width:240px" onchange="_setBslAgenticField('global_last_fallback', this.value)">
                        <option value="">— none —</option>
                        ${_getRouteOptionsHTML(cfg.global_last_fallback)}
                    </select>
                </div>
            </div>
        </div>`;

    const matrixHtml = `
        <div class="bsl-matrix-card">
            <div class="bsl-matrix-card-header">
                <div>
                    <h2>Agent Matrix</h2>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">11 agents × 3 slots (Primary / Fallback 1 / Fallback 2). Vision is a pre-flight placeholder.</div>
                </div>
                <div style="display:flex;gap:8px">
                    <button class="btn btn-outline" style="font-size:12px;padding:6px 12px;background:var(--brand-light);color:var(--brand-color);border-color:var(--brand-color);" onclick="_runAgenticAutoSelect('blacksand-agentic')" title="Fill empty agent slots from the recommended route table">⚡ Auto-Select</button>
                    <button class="btn btn-outline" style="font-size:12px;padding:6px 12px;" onclick="_clearAgenticAutoSelection('blacksand-agentic')">Clear Auto-Selection</button>
                </div>
            </div>
            ${_renderAgenticMatrix(BSL_AGENTIC_CATEGORIES, cfg.agent_routes, true, '_setBslAgenticCategorySlot', true)}
        </div>`;

    const flowHtml = `
        <div class="bsl-matrix-card">
            <div class="bsl-matrix-card-header">
                <div>
                    <h2>Resolution Chain</h2>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">How blacksand-agentic resolves a request to a concrete route</div>
                </div>
            </div>
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:13px;color:var(--text-muted);">
                <span style="padding:6px 12px;background:var(--brand-light);border-radius:8px;color:var(--brand-color);font-weight:600;">Agent P→F1→F2</span>
                <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="9 18 15 12 9 6"/></svg>
                <span style="padding:6px 12px;background:#f3f4f6;border-radius:8px;font-weight:500;">Scout Fallback P→F1→F2</span>
                <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="9 18 15 12 9 6"/></svg>
                <span style="padding:6px 12px;background:#ede9fe;border-radius:8px;color:#7c3aed;font-weight:600;">Global Last Fallback</span>
                <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="9 18 15 12 9 6"/></svg>
                <span style="padding:6px 12px;background:#fee2e2;border-radius:8px;color:var(--danger);font-weight:500;">503 No Model</span>
            </div>
            <div style="font-size:12px;color:var(--text-muted);margin-top:12px;line-height:1.6;">
                <strong>Depth = fast:</strong> Single-agent routing — classify → agent → model chain. No complexity tiers, no consult, no multi-step orchestration.<br>
                <strong>Vision:</strong> Pre-flight placeholder. Runs first if request has attachments (backend classifier pending).<br>
                <strong>Safe-default OFF:</strong> Requires both <code style="background:#f3f4f6;padding:1px 4px;border-radius:3px;">enabled</code> and <code style="background:#f3f4f6;padding:1px 4px;border-radius:3px;">tools.bsl_agentic_router</code>.
            </div>
        </div>`;

    return `${globalSectionHtml}${matrixHtml}${flowHtml}`;
}

// ─── BSL Agentic Ultra (Balanced) Render ──────────────────────────────────
function _renderBslAgenticUltraConfig() {
    const cfg = _getBslAgenticUltraCfg();

    const globalSectionHtml = `
        <div class="bsl-matrix-card">
            <div class="bsl-matrix-card-header">
                <div>
                    <h2>Blacksand Agentic Ultra — Global Configuration</h2>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">Balanced-tier coding orchestration. 17 agents, each with an independent Primary → Fallback 1 → Fallback 2 chain.</div>
                </div>
                <div style="display:flex;align-items:center;gap:12px;">
                    <span style="font-size:11px;font-weight:800;color:${cfg.enabled ? 'var(--success)' : 'var(--text-muted)'};text-transform:uppercase;">${cfg.enabled ? 'Active' : 'Paused'}</span>
                    <label class="switch">
                        <input type="checkbox" onchange="_setBslAgenticUltraEnabled(this.checked)" ${cfg.enabled ? 'checked' : ''}>
                        <span class="slider"></span>
                    </label>
                </div>
            </div>

            <div class="bsl-global-row">
                <div class="setting-info">
                    <div class="setting-title">Default Route Override</div>
                    <div class="setting-desc">When enabled, <strong>bypasses the entire agent matrix</strong>.</div>
                </div>
                <div style="display:flex;align-items:center;gap:12px">
                    <label class="switch">
                        <input type="checkbox" onchange="_setBslAgenticUltraField('default_route_enabled', this.checked); renderActiveTab()" ${cfg.default_route_enabled ? 'checked' : ''}>
                        <span class="slider"></span>
                    </label>
                    <select class="input" style="width:240px" onchange="_setBslAgenticUltraField('default_route', this.value)" ${cfg.default_route_enabled ? '' : 'disabled'}>
                        <option value="">— none —</option>
                        ${_getRouteOptionsHTML(cfg.default_route)}
                    </select>
                </div>
            </div>

            <div class="bsl-global-row">
                <div class="setting-info">
                    <div class="setting-title">Global Last Fallback</div>
                    <div class="setting-desc">Final safety net appended to every fallback chain.</div>
                </div>
                <div style="display:flex;align-items:center;gap:12px">
                    <select class="input" style="width:240px" onchange="_setBslAgenticUltraField('global_last_fallback', this.value)">
                        <option value="">— none —</option>
                        ${_getRouteOptionsHTML(cfg.global_last_fallback)}
                    </select>
                </div>
            </div>
        </div>`;

    const matrixHtml = `
        <div class="bsl-matrix-card">
            <div class="bsl-matrix-card-header">
                <div>
                    <h2>Agent Matrix</h2>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">17 agents × 3 slots. Every agent has its own independent route.</div>
                </div>
                <div style="display:flex;gap:8px">
                    <button class="btn btn-outline" style="font-size:12px;padding:6px 12px;background:var(--brand-light);color:var(--brand-color);border-color:var(--brand-color);" onclick="_runAgenticAutoSelect('blacksand-agentic-ultra')" title="Fill empty agent slots from the recommended route table">⚡ Auto-Select</button>
                    <button class="btn btn-outline" style="font-size:12px;padding:6px 12px;" onclick="_clearAgenticAutoSelection('blacksand-agentic-ultra')">Clear Auto-Selection</button>
                </div>
            </div>
            ${_renderAgenticMatrix(BSL_AGENTIC_ULTRA_CATEGORIES, cfg.agent_routes, true, '_setBslAgenticUltraCategorySlot', true)}
        </div>`;

    // NOTE: The Consult Routes panel was removed here deliberately.
    // bsl_agentic_ultra_router has NO consult stage: RouteDecision.consulted is a
    // compatibility field hardcoded to False, and
    // test_balanced_does_not_use_consult_matrix asserts consult models never enter
    // the fallback chain. Rendering the panel misrepresented routing behavior AND
    // wrote dead `consult_routes` entries into config.yaml on every change.
    // Do not reintroduce it unless the router grows a real consult stage.

    const flowHtml = `
        <div class="bsl-matrix-card">
            <div class="bsl-matrix-card-header">
                <div>
                    <h2>Resolution Chain</h2>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">How blacksand-agentic-ultra resolves a request</div>
                </div>
            </div>
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:13px;color:var(--text-muted);">
                <span style="padding:6px 12px;background:var(--brand-light);border-radius:8px;color:var(--brand-color);font-weight:600;">Agent P→F1→F2</span>
                <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="9 18 15 12 9 6"/></svg>
                <span style="padding:6px 12px;background:#f3f4f6;border-radius:8px;font-weight:500;">Scout Fallback</span>
                <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="9 18 15 12 9 6"/></svg>
                <span style="padding:6px 12px;background:#ede9fe;border-radius:8px;color:#7c3aed;font-weight:600;">Global Last Fallback</span>
                <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="9 18 15 12 9 6"/></svg>
                <span style="padding:6px 12px;background:#fee2e2;border-radius:8px;color:var(--danger);font-weight:500;">503 No Model</span>
            </div>
            <div style="font-size:12px;color:var(--text-muted);margin-top:12px;line-height:1.6;">
                <strong>Depth = balanced:</strong> Scout-first classification picks one agent, then resolves that agent's own Primary → Fallback 1 → Fallback 2 chain. Transport-only fallbacks; no second-opinion consult stage.<br>
                <strong>Safe-default OFF:</strong> Requires both <code style="background:#f3f4f6;padding:1px 4px;border-radius:3px;">enabled</code> and <code style="background:#f3f4f6;padding:1px 4px;border-radius:3px;">tools.bsl_agentic_ultra_router</code>.
            </div>
        </div>`;

    return `${globalSectionHtml}${matrixHtml}${flowHtml}`;
}

// ─── BSL Agentic Max (Multi-Domain Fusion) Render ─────────────────────────
function _renderBslAgenticMaxConfig() {
    const cfg = _getBslAgenticMaxCfg();

    const globalSectionHtml = `
        <div class="bsl-matrix-card">
            <div class="bsl-matrix-card-header">
                <div>
                    <h2>Blacksand Agentic Max — Global Configuration</h2>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">Multi-domain fusion orchestration. Combines chat + coding classification to route across domains.</div>
                </div>
                <div style="display:flex;align-items:center;gap:12px;">
                    <span style="font-size:11px;font-weight:800;color:${cfg.enabled ? 'var(--success)' : 'var(--text-muted)'};text-transform:uppercase;">${cfg.enabled ? 'Active' : 'Paused'}</span>
                    <label class="switch">
                        <input type="checkbox" onchange="_setBslAgenticMaxEnabled(this.checked)" ${cfg.enabled ? 'checked' : ''}>
                        <span class="slider"></span>
                    </label>
                </div>
            </div>

            <div class="bsl-global-row">
                <div class="setting-info">
                    <div class="setting-title">Merge Strategy</div>
                    <div class="setting-desc">How to resolve conflicts between coding and chat classification.</div>
                </div>
                <div style="display:flex;align-items:center;gap:12px">
                    <select class="input" style="width:240px" onchange="_setBslAgenticMaxField('merge_strategy', this.value)">
                        <option value="confidence_weighted" ${cfg.merge_strategy === 'confidence_weighted' ? 'selected' : ''}>Confidence Weighted</option>
                        <option value="coding_priority" ${cfg.merge_strategy === 'coding_priority' ? 'selected' : ''}>Coding Priority</option>
                        <option value="chat_priority" ${cfg.merge_strategy === 'chat_priority' ? 'selected' : ''}>Chat Priority</option>
                        <option value="dual_route" ${cfg.merge_strategy === 'dual_route' ? 'selected' : ''}>Dual Route</option>
                    </select>
                </div>
            </div>

            <div class="bsl-global-row">
                <div class="setting-info">
                    <div class="setting-title">Global Last Fallback</div>
                    <div class="setting-desc">Final safety net appended to every fallback chain.</div>
                </div>
                <div style="display:flex;align-items:center;gap:12px">
                    <select class="input" style="width:240px" onchange="_setBslAgenticMaxField('global_last_fallback', this.value)">
                        <option value="">— none —</option>
                        ${_getRouteOptionsHTML(cfg.global_last_fallback)}
                    </select>
                </div>
            </div>
        </div>`;

    // ── Read-only Chat Matrix (from blacksand-chat) ──
    const chatCfg = _getBslChatCfg();
    const chatOverrides = chatCfg.category_overrides || {};
    const chatMatrixRows = BSL_CATEGORIES.map(cat => {
        const catObj = chatOverrides[cat.id] || {};
        const cells = BSL_BUCKETS.map(bucket => {
            const cell = catObj[bucket.id] || {};
            const val = _getSlotVal(cell, 'primary');
            return `<td><span style="font-size:12px;color:${val ? 'var(--text-primary)' : 'var(--text-muted)'};font-family:monospace;">${val || '—'}</span></td>`;
        }).join('');
        return `
            <tr>
                <td>
                    <div class="bsl-cat-cell">
                        <div class="bsl-cat-icon">${cat.icon}</div>
                        ${cat.label}
                    </div>
                </td>
                ${cells}
            </tr>`;
    }).join('');

    const chatMatrixHtml = `
        <div class="bsl-matrix-card">
            <div class="bsl-matrix-card-header">
                <div>
                    <h2>Chat Matrix <span style="font-size:11px;background:#dbeafe;color:#1e40af;padding:2px 8px;border-radius:4px;font-weight:700;margin-left:8px;">READ-ONLY</span></h2>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">From Blacksand Chat. 13 categories × 3 complexity buckets. Used when chat domain wins.</div>
                </div>
                <a href="#" onclick="switchBslFamily('blacksand-chat'); return false;" style="font-size:12px;color:var(--brand-color);text-decoration:none;">Edit in Chat tab →</a>
            </div>
            <table class="bsl-matrix-table">
                <thead>
                    <tr>
                        <th style="width:200px;">Category</th>
                        <th>Fast</th>
                        <th>Standard</th>
                        <th>Deep</th>
                    </tr>
                </thead>
                <tbody>
                    ${chatMatrixRows}
                </tbody>
            </table>
        </div>`;

    // ── Read-only Agentic Matrix (from blacksand-agentic) ──
    const agenticCfg = _getBslAgenticCfg();
    const agenticRoutes = agenticCfg.agent_routes || {};
    const agenticMatrixRows = BSL_AGENTIC_CATEGORIES.map(cat => {
        const isSubAgent = !!cat.parent;
        const routeKey = cat.id;
        const agentRoute = agenticRoutes[routeKey] || {};
        const cells = BSL_SLOTS.map(s => {
            const val = _getSlotVal(agentRoute, s.id);
            return `<td><span style="font-size:12px;color:${val ? 'var(--text-primary)' : 'var(--text-muted)'};font-family:monospace;">${val || '—'}</span></td>`;
        }).join('');
        // Rows are PEERS — see the note in _renderAgenticMatrix. `parent` drives
        // backend fallback resolution only, never indentation.
        const indentStyle = '';
        return `
            <tr>
                <td>
                    <div class="bsl-cat-cell" style="${indentStyle}">
                        <div class="bsl-cat-icon">${cat.icon}</div>
                        ${cat.label}
                    </div>
                </td>
                ${cells}
            </tr>`;
    }).join('');

    const agenticMatrixHtml = `
        <div class="bsl-matrix-card">
            <div class="bsl-matrix-card-header">
                <div>
                    <h2>Agentic Matrix <span style="font-size:11px;background:#dbeafe;color:#1e40af;padding:2px 8px;border-radius:4px;font-weight:700;margin-left:8px;">READ-ONLY</span></h2>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">From Blacksand Agentic. 11 agents × 3 slots. Used when coding domain wins.</div>
                </div>
                <a href="#" onclick="switchBslFamily('blacksand-agentic'); return false;" style="font-size:12px;color:var(--brand-color);text-decoration:none;">Edit in Agentic tab →</a>
            </div>
            <table class="bsl-matrix-table">
                <thead>
                    <tr>
                        <th style="width:280px;">Agent</th>
                        <th>Primary</th>
                        <th>Fallback 1</th>
                        <th>Fallback 2</th>
                    </tr>
                </thead>
                <tbody>
                    ${agenticMatrixRows}
                </tbody>
            </table>
        </div>`;

    const flowHtml = `
        <div class="bsl-matrix-card">
            <div class="bsl-matrix-card-header">
                <div>
                    <h2>Resolution Chain</h2>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">How blacksand-agentic-max resolves a request</div>
                </div>
            </div>
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:13px;color:var(--text-muted);">
                <span style="padding:6px 12px;background:#fef3c7;border-radius:8px;color:#92400e;font-weight:600;">Coding Classifier</span>
                <span style="font-size:16px;">+</span>
                <span style="padding:6px 12px;background:#dbeafe;border-radius:8px;color:#1e40af;font-weight:600;">Chat Classifier</span>
                <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="9 18 15 12 9 6"/></svg>
                <span style="padding:6px 12px;background:#ede9fe;border-radius:8px;color:#7c3aed;font-weight:600;">Merge Strategy</span>
                <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="9 18 15 12 9 6"/></svg>
                <span style="padding:6px 12px;background:var(--brand-light);border-radius:8px;color:var(--brand-color);font-weight:600;">Domain Route P→F1→F2</span>
                <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="9 18 15 12 9 6"/></svg>
                <span style="padding:6px 12px;background:#ede9fe;border-radius:8px;color:#7c3aed;font-weight:600;">Global Last Fallback</span>
                <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="9 18 15 12 9 6"/></svg>
                <span style="padding:6px 12px;background:#fee2e2;border-radius:8px;color:var(--danger);font-weight:500;">503 No Model</span>
            </div>
            <div style="font-size:12px;color:var(--text-muted);margin-top:12px;line-height:1.6;">
                <strong>Multi-domain fusion:</strong> Both classifiers run in parallel. The merge strategy picks the winning domain, and that domain's matrix resolves the route.<br>
                <strong>Coding wins:</strong> Routes through the agentic matrix (8 agents).<br>
                <strong>Chat wins:</strong> Routes through the chat matrix (13 categories × 3 buckets).<br>
                <strong>Safe-default OFF:</strong> Requires both <code style="background:#f3f4f6;padding:1px 4px;border-radius:3px;">enabled</code> and <code style="background:#f3f4f6;padding:1px 4px;border-radius:3px;">tools.bsl_agentic_max_router</code>.
            </div>
        </div>`;

    return `${globalSectionHtml}${chatMatrixHtml}${agenticMatrixHtml}${flowHtml}`;
}

function _renderBslLiteConfig() {
    const cfg = _getBslLiteCfg();

    // ── Global configuration card ──
    const globalSectionHtml = `
        <div class="bsl-matrix-card">
            <div class="bsl-matrix-card-header">
                <div>
                    <h2>Blacksand Lite — Global Configuration</h2>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">Coding-agent single-task router (Claude Code, Cursor, Aider). 8-agent flat matrix — no complexity tiers, no multi-step orchestration.</div>
                </div>
                <div style="display:flex;align-items:center;gap:12px;">
                    <span style="font-size:11px;font-weight:800;color:${cfg.enabled ? 'var(--success)' : 'var(--text-muted)'};text-transform:uppercase;">${cfg.enabled ? 'Active' : 'Paused'}</span>
                    <label class="switch">
                        <input type="checkbox" onchange="_setBslLiteEnabled(this.checked)" ${cfg.enabled ? 'checked' : ''}>
                        <span class="slider"></span>
                    </label>
                </div>
            </div>

            <div class="bsl-global-row">
                <div class="setting-info">
                    <div class="setting-title">Default Route Override</div>
                    <div class="setting-desc">When enabled, <strong>bypasses the entire agent matrix</strong> — ALL requests route to this one model. Use this to simplify routing when you don't need per-agent intelligence.</div>
                </div>
                <div style="display:flex;align-items:center;gap:12px">
                    <label class="switch">
                        <input type="checkbox" onchange="_setBslLiteField('default_route_enabled', this.checked); renderActiveTab()" ${cfg.default_route_enabled ? 'checked' : ''}>
                        <span class="slider"></span>
                    </label>
                    <select class="input" style="width:240px" onchange="_setBslLiteField('default_route', this.value)" ${cfg.default_route_enabled ? '' : 'disabled'}>
                        <option value="">— none —</option>
                        ${_getRouteOptionsHTML(cfg.default_route)}
                    </select>
                </div>
            </div>

            <div class="bsl-global-row">
                <div class="setting-info">
                    <div class="setting-title">Global Last Fallback</div>
                    <div class="setting-desc">Final safety net appended to every fallback chain. Always active when configured — no on/off toggle. Should be a reliable, always-available model.</div>
                </div>
                <div style="display:flex;align-items:center;gap:12px">
                    <select class="input" style="width:240px" onchange="_setBslLiteField('global_last_fallback', this.value)">
                        <option value="">— none —</option>
                        ${_getRouteOptionsHTML(cfg.global_last_fallback)}
                    </select>
                </div>
            </div>
        </div>`;

    // ── 8-agent flat matrix (Primary / Fallback 1 / Fallback 2 per agent) ──
    const categoryOverrides = cfg.category_overrides || {};
    const matrixRowsHtml = BSL_LITE_CATEGORIES.map(cat => {
        const agentRoute = categoryOverrides[cat.id] || {};
        const slotsHtml = BSL_SLOTS.map(s => {
            const val = _getSlotVal(agentRoute, s.id);
            const markerKey = `${cat.id}/${s.id}`;
            const isAutoMarked = cfg.auto_selected_slots && cfg.auto_selected_slots[markerKey] === true;
            const selectClass = `bsl-slot-select ${val ? (isAutoMarked ? 'auto-marked' : 'has-override') : 'inherited'}`;
            return `
                <td>
                    <select class="${selectClass}" onchange="_setBslLiteCategorySlot('${cat.id}', '${s.id}', this.value)" title="${val ? (isAutoMarked ? 'Auto-selected — edit to make manual' : 'Override active') : 'Empty'}">
                        <option value="">— none —</option>
                        ${_getRouteOptionsHTML(val)}
                    </select>
                </td>`;
        }).join('');
        return `
            <tr>
                <td>
                    <div class="bsl-cat-cell">
                        <div class="bsl-cat-icon">${cat.icon}</div>
                        ${cat.label}
                    </div>
                </td>
                ${slotsHtml}
            </tr>`;
    }).join('');

    const matrixHtml = `
        <div class="bsl-matrix-card">
            <div class="bsl-matrix-card-header">
                <div>
                    <h2>Agent Matrix</h2>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">8 OAC agents × 3 slots (Primary / Fallback 1 / Fallback 2) — no complexity tiers, each slot accepts combo or provider model</div>
                </div>
                <div style="display:flex;gap:8px">
                    <button class="btn btn-outline" style="font-size:12px;padding:6px 12px;background:var(--brand-light);color:var(--brand-color);border-color:var(--brand-color);" onclick="_runBslLiteAutoSelect()" title="Fill empty slots from the predefined benchmark-backed recommendations">⚡ Auto-Select</button>
                    <button class="btn btn-outline" style="font-size:12px;padding:6px 12px;" onclick="_clearAllBslLiteOverrides()">Clear Auto-Selection</button>
                </div>
            </div>
            <table class="bsl-matrix-table">
                <thead>
                    <tr>
                        <th>Agent</th>
                        <th>Primary</th>
                        <th>Fallback 1</th>
                        <th>Fallback 2</th>
                    </tr>
                </thead>
                <tbody>
                    ${matrixRowsHtml}
                </tbody>
            </table>
        </div>`;

    // ── Routing logic visualization ──
    const flowHtml = `
        <div class="bsl-matrix-card">
            <div class="bsl-matrix-card-header">
                <div>
                    <h2>Resolution Chain</h2>
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">How bsl-lite resolves a request to a concrete route</div>
                </div>
            </div>
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:13px;color:var(--text-muted);">
                ${cfg.default_route_enabled
                    ? `<span style="padding:6px 12px;background:#ede9fe;border-radius:8px;color:#7c3aed;font-weight:600;">Default Route</span>
                       <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="9 18 15 12 9 6"/></svg>
                       <span style="padding:6px 12px;background:#ede9fe;border-radius:8px;color:#7c3aed;font-weight:600;">Global Last Fallback</span>`
                    : `<span style="padding:6px 12px;background:var(--brand-light);border-radius:8px;color:var(--brand-color);font-weight:600;">Agent P→F1→F2</span>
                       <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="9 18 15 12 9 6"/></svg>
                       <span style="padding:6px 12px;background:#f3f4f6;border-radius:8px;font-weight:500;">Scout Fallback P→F1→F2</span>
                       <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="9 18 15 12 9 6"/></svg>
                       <span style="padding:6px 12px;background:#ede9fe;border-radius:8px;color:#7c3aed;font-weight:600;">Global Last Fallback</span>`
                }
                <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="9 18 15 12 9 6"/></svg>
                <span style="padding:6px 12px;background:#fee2e2;border-radius:8px;color:var(--danger);font-weight:500;">503 No Model</span>
            </div>
            <div style="font-size:12px;color:var(--text-muted);margin-top:12px;line-height:1.6;">
                ${cfg.default_route_enabled
                    ? '<strong>Default Route ON:</strong> All Blacksand Lite requests bypass the agent matrix and use the single selected route. If it fails, Global Last Fallback is tried, then 503.'
                    : '<strong>Default Route OFF:</strong> Requests flow through Agent (P→F1→F2) → Scout Fallback (P→F1→F2) → Global Last Fallback → 503.'
                }<br>
                <strong>Global Last Fallback:</strong> Always active when configured — no disable toggle.<br>
                <strong>Default-off safety:</strong> Smart routing is <strong>disabled</strong> unless both <code style="background:#f3f4f6;padding:1px 4px;border-radius:3px;">enabled</code> and <code style="background:#f3f4f6;padding:1px 4px;border-radius:3px;">tools.bsl_lite_router</code> are <code style="background:#f3f4f6;padding:1px 4px;border-radius:3px;">true</code>.
            </div>
        </div>`;

    return `
        ${globalSectionHtml}
        ${matrixHtml}
        ${flowHtml}`;
}

// ── BSL-Lite Event Handlers ──

window._setBslLiteEnabled = function(enabled) {
    const cfg = _getBslLiteCfg();
    cfg.enabled = enabled;
    if (!globalConfig.tools) globalConfig.tools = {};
    globalConfig.tools.bsl_lite_router = enabled;
    renderActiveTab();
};

window._setBslLiteField = function(field, value) {
    const cfg = _getBslLiteCfg();
    cfg[field] = value || undefined;
};

// ── BSL-Lite Matrix Event Handlers ──

// Category override slot setter for bsl-lite — flat agent→{primary,fallback_1,fallback_2}
window._setBslLiteCategorySlot = function(category, slot, value) {
    const cfg = _getBslLiteCfg();
    if (!cfg.category_overrides) cfg.category_overrides = {};
    if (!cfg.category_overrides[category]) cfg.category_overrides[category] = {};
    if (value) {
        cfg.category_overrides[category][slot] = value;
    } else {
        delete cfg.category_overrides[category][slot];
    }
    // Clean up empty agent objects
    if (Object.keys(cfg.category_overrides[category]).length === 0) {
        delete cfg.category_overrides[category];
    }
    // A manual edit converts an auto-marked slot into ordinary manual state.
    const markerKey = `${category}/${slot}`;
    if (cfg.auto_selected_slots) delete cfg.auto_selected_slots[markerKey];
    renderActiveTab();
};

window._clearAllBslLiteOverrides = function() {
    const cfg = _getBslLiteCfg();
    if (!confirm('Clear auto-selected slots? This removes only slots filled by Auto-Select that have not been manually edited. Manual values are preserved.')) return;
    const markers = cfg.auto_selected_slots || {};
    const markerKeys = Object.keys(markers);
    let cleared = 0;

    // Pass 1: clear marker-tagged slots.
    for (const markerKey of markerKeys) {
        const [category, slot] = markerKey.split('/');
        const agentRoute = (cfg.category_overrides && cfg.category_overrides[category]) || null;
        // Only clear the slot if it is still marked as auto-selected.
        if (agentRoute && agentRoute[slot] && markers[markerKey] === true) {
            delete agentRoute[slot];
            cleared++;
            if (Object.keys(agentRoute).length === 0) {
                delete cfg.category_overrides[category];
            }
        }
    }

    // Pass 2: orphan sweep — clear values matching the benchmark
    // recommendation even without a marker (see _clearAllBslOverrides).
    _fetchRecommendedMatrix('/api/bsl-lite-matrix/auto-select-preview').then(matrix => {
        if (matrix) {
            for (const [category, tiers] of Object.entries(matrix)) {
                const cellData = (tiers && typeof tiers === 'object') ? (tiers.standard || tiers.fast || tiers.deep || {}) : {};
                const agentRoute = (cfg.category_overrides && cfg.category_overrides[category]) || null;
                if (!agentRoute) continue;
                for (const slot of Object.keys(agentRoute)) {
                    const rec = (cellData && cellData[slot] && cellData[slot].route_id) || '';
                    if (rec && agentRoute[slot] === rec) {
                        delete agentRoute[slot];
                        cleared++;
                    }
                }
                if (Object.keys(agentRoute).length === 0) {
                    delete cfg.category_overrides[category];
                }
            }
        } else {
            showToast('Warning: recommendation fetch failed — cleared only marker-tagged slots', true);
        }
        cfg.auto_selected_slots = {};
        renderActiveTab();
        scheduleAutoSave();
        showToast(`Cleared ${cleared} auto-selected slot${cleared !== 1 ? 's' : ''}`);
    });
};

window._runBslLiteAutoSelect = async function() {
    showToast('Running benchmark-powered auto-selection for Blacksand Lite...');
    try {
        const resp = await fetch('/api/bsl-lite-matrix/auto-select-preview', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({})
        });
        if (!resp.ok) {
            let detail = `HTTP ${resp.status}`;
            try { const err = await resp.json(); if (err && err.error) detail = err.error; } catch (e) { /* ignore */ }
            showToast(`Auto-select failed: ${detail}`, true);
            return;
        }
        const matrix = await resp.json();
        const cfg = _getBslLiteCfg();
        if (!cfg.category_overrides) cfg.category_overrides = {};
        if (!cfg.auto_selected_slots) cfg.auto_selected_slots = {};
        let filled = 0;
        for (const [cat, tiers] of Object.entries(matrix)) {
            // BSL-Lite is flat (no tiers) — use the standard tier as the canonical recommendation
            const cellData = (tiers && typeof tiers === 'object') ? (tiers.standard || tiers.fast || tiers.deep || {}) : {};
            if (!cfg.category_overrides[cat]) cfg.category_overrides[cat] = {};
            const existing = cfg.category_overrides[cat];
            let agentFilled = false;
            for (const slot of BSL_SLOTS.map(s => s.id)) {
                if (existing[slot]) continue;
                if (cellData && cellData[slot] && cellData[slot].route_id) {
                    existing[slot] = cellData[slot].route_id;
                    cfg.auto_selected_slots[`${cat}/${slot}`] = true;
                    agentFilled = true;
                }
            }
            if (agentFilled) filled++;
        }
        renderActiveTab();
        scheduleAutoSave();
        showToast(`Auto-select complete: ${filled} agent${filled !== 1 ? 's' : ''} filled`);
    } catch (e) {
        showToast(`Auto-select failed: ${e.message}`, true);
    }
};

// ── BSL-Agentic Event Handlers ──

window._setBslAgenticEnabled = function(enabled) {
    const cfg = _getBslAgenticCfg();
    cfg.enabled = enabled;
    if (!globalConfig.tools) globalConfig.tools = {};
    globalConfig.tools.bsl_agentic_router = enabled;
    renderActiveTab();
};

window._setBslAgenticField = function(field, value) {
    const cfg = _getBslAgenticCfg();
    cfg[field] = value || undefined;
};

window._setBslAgenticCategorySlot = function(agent, slot, value) {
    const cfg = _getBslAgenticCfg();
    if (!cfg.agent_routes) cfg.agent_routes = {};
    if (!cfg.agent_routes[agent]) cfg.agent_routes[agent] = {};
    if (value) {
        cfg.agent_routes[agent][slot] = value;
    } else {
        delete cfg.agent_routes[agent][slot];
    }
    if (cfg.auto_selected_slots) delete cfg.auto_selected_slots[`${agent}/${slot}`];
    if (Object.keys(cfg.agent_routes[agent]).length === 0) {
        delete cfg.agent_routes[agent];
    }
    renderActiveTab();
};

// ── BSL-Agentic Ultra Event Handlers ──

window._setBslAgenticUltraEnabled = function(enabled) {
    const cfg = _getBslAgenticUltraCfg();
    cfg.enabled = enabled;
    if (!globalConfig.tools) globalConfig.tools = {};
    globalConfig.tools.bsl_agentic_ultra_router = enabled;
    renderActiveTab();
};

window._setBslAgenticUltraField = function(field, value) {
    const cfg = _getBslAgenticUltraCfg();
    cfg[field] = value || undefined;
};

window._setBslAgenticUltraCategorySlot = function(agent, slot, value) {
    const cfg = _getBslAgenticUltraCfg();
    if (!cfg.agent_routes) cfg.agent_routes = {};
    if (!cfg.agent_routes[agent]) cfg.agent_routes[agent] = {};
    if (value) {
        cfg.agent_routes[agent][slot] = value;
        // Manual edit: drop the auto-select marker so Clear preserves this value.
        if (cfg.auto_selected_slots) delete cfg.auto_selected_slots[`${agent}/${slot}`];
    } else {
        delete cfg.agent_routes[agent][slot];
    }
    if (Object.keys(cfg.agent_routes[agent]).length === 0) {
        delete cfg.agent_routes[agent];
    }
    renderActiveTab();
};

// _setBslAgenticUltraConsultSlot was removed with the Consult Routes panel.
// bsl_agentic_ultra_router has no consult stage, so this handler only wrote
// dead `consult_routes` entries into config.yaml.

// ── BSL-Agentic Max Event Handlers ──

window._setBslAgenticMaxEnabled = function(enabled) {
    const cfg = _getBslAgenticMaxCfg();
    cfg.enabled = enabled;
    if (!globalConfig.tools) globalConfig.tools = {};
    globalConfig.tools.bsl_agentic_max_router = enabled;
    renderActiveTab();
};

window._setBslAgenticMaxField = function(field, value) {
    const cfg = _getBslAgenticMaxCfg();
    cfg[field] = value || undefined;
};

window._setBslChatField = function(field, value) {
    const cfg = _getBslChatCfg();
    cfg[field] = value || undefined;
};

// Category override slot setter — works with 3-slot cell objects
window._setCategorySlot = function(category, bucket, slot, value) {
    const cfg = _getBslChatCfg();
    if (!cfg.category_overrides) cfg.category_overrides = {};
    if (!cfg.category_overrides[category]) cfg.category_overrides[category] = {};
    if (!cfg.category_overrides[category][bucket]) cfg.category_overrides[category][bucket] = {};
    if (value) {
        cfg.category_overrides[category][bucket][slot] = value;
    } else {
        delete cfg.category_overrides[category][bucket][slot];
    }
    // Clean up empty objects (bucket → category)
    if (Object.keys(cfg.category_overrides[category][bucket]).length === 0) {
        delete cfg.category_overrides[category][bucket];
    }
    if (Object.keys(cfg.category_overrides[category]).length === 0) {
        delete cfg.category_overrides[category];
    }
    // A manual edit converts an auto-marked slot into ordinary manual state.
    const markerKey = `${category}/${bucket}/${slot}`;
    if (cfg.auto_selected_slots) delete cfg.auto_selected_slots[markerKey];
    renderActiveTab();
};

// Fetch the recommended matrix for orphan detection (Chat: 8 cats x 3 tiers).
async function _fetchRecommendedMatrix(url) {
    try {
        const resp = await fetch(url, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({})
        });
        if (!resp.ok) return null;
        return await resp.json();
    } catch (e) {
        console.warn(`[ClearAutoSelect] recommendation fetch failed: ${e.message}`);
        return null;
    }
}

window._clearAllBslOverrides = function() {
    const cfg = _getBslChatCfg();
    if (!confirm('Clear auto-selected slots? This removes only slots filled by Auto-Select that have not been manually edited. Manual values are preserved.')) return;
    const markers = cfg.auto_selected_slots || {};
    const markerKeys = Object.keys(markers);
    let cleared = 0;

    // Pass 1: clear marker-tagged slots.
    for (const markerKey of markerKeys) {
        const [category, bucket, slot] = markerKey.split('/');
        const cell = (cfg.category_overrides && cfg.category_overrides[category])
            ? cfg.category_overrides[category][bucket] : null;
        // Only clear the slot if it is still marked as auto-selected.
        // A manual edit would have already removed the marker, so this
        // double-check guarantees manual values are never touched.
        if (cell && cell[slot] && markers[markerKey] === true) {
            delete cell[slot];
            cleared++;
            // Clean up empty containers to avoid stale empty shells
            if (Object.keys(cell).length === 0 && cfg.category_overrides[category]) {
                delete cfg.category_overrides[category][bucket];
                if (Object.keys(cfg.category_overrides[category]).length === 0) {
                    delete cfg.category_overrides[category];
                }
            }
        }
    }

    // Pass 2: orphan sweep — clear any populated slot whose value exactly
    // matches the benchmark recommendation even when no marker exists
    // (markers were memory-only before the scheduleAutoSave fix, so values
    // became orphaned and previously unclearable). Manual values are only
    // touched when they match the recommendation for that exact slot.
    _fetchRecommendedMatrix('/api/bsl-matrix/auto-select-preview').then(matrix => {
        if (matrix) {
            for (const [category, tiers] of Object.entries(matrix)) {
                for (const [bucket, cellData] of Object.entries(tiers)) {
                    const cell = (cfg.category_overrides && cfg.category_overrides[category])
                        ? cfg.category_overrides[category][bucket] : null;
                    if (!cell) continue;
                    for (const slot of Object.keys(cell)) {
                        const rec = (cellData && cellData[slot] && cellData[slot].route_id) || '';
                        if (rec && cell[slot] === rec) {
                            delete cell[slot];
                            cleared++;
                        }
                    }
                    if (Object.keys(cell).length === 0 && cfg.category_overrides[category]) {
                        delete cfg.category_overrides[category][bucket];
                        if (Object.keys(cfg.category_overrides[category]).length === 0) {
                            delete cfg.category_overrides[category];
                        }
                    }
                }
            }
        } else {
            showToast('Warning: recommendation fetch failed — cleared only marker-tagged slots', true);
        }
        // Remove all markers (they were either cleared or already gone)
        cfg.auto_selected_slots = {};
        renderActiveTab();
        scheduleAutoSave();
        showToast(`Cleared ${cleared} auto-selected slot${cleared !== 1 ? 's' : ''}`);
    });
};

// ─── BSL Agentic Auto-Select (Fast / Ultra / Max) ────────────────────────────
// Recommended route table for agent matrices. Agents have no benchmark-backed
// auto-select endpoint (that is category×complexity only), so Agentic tabs use
// this curated static mapping: coder-1 = fast/cheap, coder-2 = standard,
// coder-3 = strongest. Mirrors bsl_agentic_router.py's docstring defaults.
const BSL_AGENTIC_RECOMMENDED_ROUTES = {
    vision:         { primary: 'coder-2', fallback_1: 'coder-1', fallback_2: 'coder-3' },
    scout:          { primary: 'coder-1', fallback_1: 'coder-2', fallback_2: '' },
    fast_coder:     { primary: 'coder-1', fallback_1: 'coder-2', fallback_2: '' },
    power_coder:    { primary: 'coder-2', fallback_1: 'coder-3', fallback_2: '' },
    ultra_coder:    { primary: 'coder-3', fallback_1: 'coder-2', fallback_2: '' },
    refactor:       { primary: 'coder-2', fallback_1: 'coder-3', fallback_2: '' },
    frontend_coder: { primary: 'coder-2', fallback_1: 'coder-1', fallback_2: '' },
    // Sub-agents: distinct role-appropriate routes (previously skipped entirely,
    // which is why every sub-agent showed a parent clone after Auto-Select).
    planner_architect:         { primary: 'coder-3', fallback_1: 'coder-2', fallback_2: '' },
    planner_challenger:        { primary: 'coder-3', fallback_1: 'coder-2', fallback_2: '' },
    planner_challenger_member: { primary: 'coder-3', fallback_1: 'coder-2', fallback_2: '' },
    planner_planner:           { primary: 'coder-2', fallback_1: 'coder-3', fallback_2: '' },
    auditor_reviewer:          { primary: 'coder-3', fallback_1: 'coder-2', fallback_2: '' },
    auditor_reviewer_member:   { primary: 'coder-3', fallback_1: 'coder-2', fallback_2: '' },
    auditor_auditor:           { primary: 'coder-3', fallback_1: 'coder-2', fallback_2: '' },
    auditor_auditor_member:    { primary: 'coder-3', fallback_1: 'coder-2', fallback_2: '' },
};
const _AGENTIC_CFG_GETTERS = {
    'blacksand-agentic':       _getBslAgenticCfg,
    'blacksand-agentic-ultra': _getBslAgenticUltraCfg,
    'blacksand-agentic-max':   _getBslAgenticMaxCfg,
};

// Fill EMPTY agent-route slots from the benchmark-backed auto-select endpoint
// (/api/bsl-agentic-matrix/auto-select-preview). The endpoint uses the real
// BSL Router model pool and enforces one canonical family per slot (the
// engine dedupes families across P/F1/F2). Manual values are never
// overwritten; filled slots are marked so Clear can remove only auto-selected
// values (same semantics as Blacksand Chat). Fails open to the legacy static table
// when the endpoint is unavailable.
window._runAgenticAutoSelect = async function(family) {
    const getCfg = _AGENTIC_CFG_GETTERS[family];
    const categories = family === 'blacksand-agentic-ultra' ? BSL_AGENTIC_ULTRA_CATEGORIES
        : family === 'blacksand-agentic-max' ? (typeof BSL_AGENTIC_MAX_CATEGORIES !== 'undefined' ? BSL_AGENTIC_MAX_CATEGORIES : BSL_AGENTIC_CATEGORIES)
        : BSL_AGENTIC_CATEGORIES;

    if (!getCfg || !categories) return;
    const cfg = getCfg();
    if (!cfg.agent_routes) cfg.agent_routes = {};
    if (!cfg.auto_selected_slots) cfg.auto_selected_slots = {};
    let matrix = null;
    try {
        const resp = await fetch('/api/bsl-agentic-matrix/auto-select-preview', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({})
        });
        if (resp.ok) {
            matrix = await resp.json();
        } else {
            let detail = `HTTP ${resp.status}`;
            try { const err = await resp.json(); if (err && err.error) detail = err.error; } catch (e) { /* ignore */ }
            console.warn(`[AgenticAutoSelect] preview endpoint failed: ${detail} — falling back to static table`);
        }
    } catch (e) {
        console.warn(`[AgenticAutoSelect] preview endpoint error: ${e.message} — falling back to static table`);
    }
    let applied = 0;
    for (const agent of categories) {
        let rec = null;
        if (matrix && matrix[agent.id] && matrix[agent.id].standard) {
            const cellData = matrix[agent.id].standard;
            rec = {};
            for (const slot of BSL_SLOTS.map(s => s.id)) {
                if (cellData[slot] && cellData[slot].route_id) rec[slot] = cellData[slot].route_id;
            }
        }
        if (!rec) rec = BSL_AGENTIC_RECOMMENDED_ROUTES[agent.id];
        if (!rec) continue;
        if (!cfg.agent_routes[agent.id]) cfg.agent_routes[agent.id] = {};
        const existing = cfg.agent_routes[agent.id];
        for (const slot of BSL_SLOTS.map(s => s.id)) {
            if (!rec[slot]) continue;
            if (existing[slot] && !cfg.auto_selected_slots[`${agent.id}/${slot}`]) continue; // never overwrite manual values
            existing[slot] = rec[slot];
            cfg.auto_selected_slots[`${agent.id}/${slot}`] = true;
            applied++;
        }
    }
    window._activeAgenticAutoMarkers = cfg.auto_selected_slots;
    renderActiveTab();
    scheduleAutoSave();
    showToast(`Auto-select applied ${applied} route slot${applied !== 1 ? 's' : ''}`);
};

// Clear ONLY slots that were auto-selected and not subsequently edited by hand.
window._clearAgenticAutoSelection = function(family) {
    const getCfg = _AGENTIC_CFG_GETTERS[family];
    if (!getCfg) return;
    const cfg = getCfg();
    if (!confirm('Clear auto-selected slots? Only slots filled by Auto-Select that have not been manually edited are removed. Manual values are preserved.')) return;
    const markers = cfg.auto_selected_slots || {};
    const markerKeys = Object.keys(markers);
    let cleared = 0;

    // Pass 1: clear marker-tagged slots.
    for (const markerKey of markerKeys) {
        const [agent, slot] = markerKey.split('/');
        const route = (cfg.agent_routes || {})[agent];
        if (route && route[slot] && markers[markerKey] === true) {
            delete route[slot];
            cleared++;
            if (Object.keys(route).length === 0) delete cfg.agent_routes[agent];
        }
    }

    // Pass 2: orphan sweep — clear any populated slot whose value exactly
    // matches a benchmark recommendation even when no marker exists.
    // Agentic recommended set comes from the agentic preview endpoint; the
    // static BSL_AGENTIC_RECOMMENDED_ROUTES table is the fallback.
    _fetchRecommendedMatrix('/api/bsl-agentic-matrix/auto-select-preview').then(matrix => {
        for (const [agent, route] of Object.entries(cfg.agent_routes || {})) {
            const cellData = (matrix && matrix[agent] && matrix[agent].standard) ? matrix[agent].standard : (BSL_AGENTIC_RECOMMENDED_ROUTES[agent] || {});
            if (!cellData) continue;
            for (const slot of Object.keys(route)) {
                const rec = (typeof cellData[slot] === 'string' ? cellData[slot] : (cellData[slot] && cellData[slot].route_id)) || '';
                if (rec && route[slot] === rec) {
                    delete route[slot];
                    cleared++;
                }
            }
            if (Object.keys(route).length === 0) delete cfg.agent_routes[agent];
        }
        if (!matrix) {
            showToast('Warning: recommendation fetch failed — cleared only marker-tagged slots', true);
        }
        cfg.auto_selected_slots = {};
        renderActiveTab();
        scheduleAutoSave();
        showToast(`Cleared ${cleared} auto-selected slot${cleared !== 1 ? 's' : ''}`);
    });
};

window.switchBslFamily = function(familyId) {
    _activeBslFamily = familyId;
    renderActiveTab();
};

window._runAutoSelect = async function() {
    showToast('Running benchmark-powered auto-selection...');
    try {
        const resp = await fetch('/api/bsl-matrix/auto-select-preview', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({})
        });
        if (!resp.ok) {
            let detail = `HTTP ${resp.status}`;
            try { const err = await resp.json(); if (err && err.error) detail = err.error; } catch (e) { /* ignore */ }
            showToast(`Auto-select failed: ${detail}`, true);
            return;
        }
        const matrix = await resp.json();
        const cfg = _getBslChatCfg();
        if (!cfg.category_overrides) cfg.category_overrides = {};
        if (!cfg.auto_selected_slots) cfg.auto_selected_slots = {};
        let filled = 0;
        for (const [cat, tiers] of Object.entries(matrix)) {
            if (!cfg.category_overrides[cat]) cfg.category_overrides[cat] = {};
            for (const [tier, cellData] of Object.entries(tiers)) {
                if (!cfg.category_overrides[cat][tier]) cfg.category_overrides[cat][tier] = {};
                const existing = cfg.category_overrides[cat][tier];
                let cellFilled = false;
                for (const slot of BSL_SLOTS.map(s => s.id)) {
                    if (existing[slot]) continue;
                    if (cellData && cellData[slot] && cellData[slot].route_id) {
                        existing[slot] = cellData[slot].route_id;
                        cfg.auto_selected_slots[`${cat}/${tier}/${slot}`] = true;
                        cellFilled = true;
                    }
                }
                if (cellFilled) filled++;
            }
        }
        renderActiveTab();
        scheduleAutoSave();
        showToast(`Auto-select complete: ${filled} empty cells filled`);
    } catch (e) {
        showToast(`Auto-select failed: ${e.message}`, true);
    }
};

function renderSettingsTab() {
    const admin = globalConfig.admin || {};
    const passwordEnabled = admin.password_enabled === true;
    const currentPassword = admin.password || '123456';
    const watchdog = globalConfig.watchdog || {};
    const watchdogEnabled = watchdog.auto_restart === true;

    return `
        <div class="settings-section">
            <h2 class="section-title">🔐 Admin Security</h2>

            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Password Protection</div>
                    <div class="setting-desc">When enabled, every BSL Router restart requires a password to access this admin panel. Proxy traffic is NOT affected — only admin panel access is gated.</div>
                </div>
                <label class="switch">
                    <input type="checkbox" id="admin-password-toggle" onchange="toggleAdminPassword(this.checked)" ${passwordEnabled ? 'checked' : ''}>
                    <span class="slider"></span>
                </label>
            </div>

            <div id="admin-password-field" style="display:${passwordEnabled ? 'flex' : 'none'};align-items:center;gap:12px;padding-top:16px;">
                <div style="flex:1;">
                    <label style="font-size:13px;font-weight:500;color:var(--text-main);margin-bottom:6px;display:block;">Admin Password</label>
                    <input type="text" class="input" id="admin-password-input" value="${currentPassword}" placeholder="Enter password (min 6 characters)" style="max-width:320px;">
                    <div style="font-size:12px;color:var(--text-muted);margin-top:4px;">Default: 123456. Must be at least 6 characters. Saved when you click Save Changes.</div>
                </div>
            </div>
        </div>

        <div class="settings-section">
            <h2 class="section-title">⚡ System Controls</h2>

            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Shutdown BSL Router</div>
                    <div class="setting-desc">Stops the backend service immediately. Only BSL Router is affected — other applications (MITM proxy, tunnels, etc.) continue running. You'll need to restart BSL Router manually.</div>
                </div>
                <button class="btn-shutdown" onclick="handleShutdown()">
                    <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><path d="M18.36 6.64l-1.42-1.42"/><path d="M12 2v10"/><path d="M5.64 6.64l1.42-1.42"/><path d="M2 12h2"/><path d="M20 12h2"/><circle cx="12" cy="12" r="4"/></svg>
                    Shutdown
                </button>
            </div>

            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Logout</div>
                    <div class="setting-desc">Clears your admin session. The BSL Router service keeps running normally — you'll need to re-enter the password to access this panel again.</div>
                </div>
                <button class="btn-logout" onclick="handleAdminLogout()">
                    <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
                    Logout
                </button>
            </div>
        </div>

        <div class="settings-section">
            <h2 class="section-title">🛡️ Anti-Freeze & Stream Recovery</h2>

            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Active Streams</div>
                    <div class="setting-desc">Live count of in-flight SSE stream tasks. If this climbs and requests hang, a stuck stream may be blocking the pipeline.</div>
                </div>
                <div style="display:flex; align-items:center; gap:12px;">
                    <span id="afz-active-count" class="badge" style="font-size:14px; font-weight:700; padding:4px 12px; background:var(--bg-surface); border:1px solid var(--border-color); border-radius:8px;">—</span>
                    <button class="btn-shutdown" id="afz-force-stop-btn" onclick="handleForceStopStreams()">
                        <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>
                        Force-Stop All Streams
                    </button>
                </div>
            </div>

            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Auto-Restart on Freeze</div>
                    <div class="setting-desc">When enabled, a background watchdog polls the router health endpoint every 5 seconds. If it fails 3 times in a row (15s of total unresponsiveness), the router process is automatically killed and restarted. This only triggers when the event loop itself is frozen — model errors, stream stalls, and high load do NOT trigger a restart. In-flight calls during restart are lost (not retried). Requires router restart to take effect.</div>
                </div>
                <label class="switch">
                    <input type="checkbox" id="afz-auto-restart-toggle" onchange="toggleAutoRestart(this.checked)" ${watchdogEnabled ? 'checked' : ''}>
                    <span class="slider"></span>
                </label>
            </div>
        </div>
    `;
}

/**
 * Toggle admin password protection.
 * Shows/hides the password input field.
 */
function toggleAdminPassword(enabled) {
    const field = document.getElementById('admin-password-field');
    if (field) field.style.display = enabled ? 'flex' : 'none';

    // Update globalConfig
    if (!globalConfig.admin) globalConfig.admin = {};
    globalConfig.admin.password_enabled = enabled;

    // If enabling and no password set, use default
    if (enabled) {
        const input = document.getElementById('admin-password-input');
        if (input && !input.value) {
            input.value = '123456';
            globalConfig.admin.password = '123456';
        }
    }
}

// ── Save hook: persist admin password before saving config ──
const _originalSaveConfig = saveConfig;
saveConfig = async function() {
    // Sync admin password from input to globalConfig before save
    const pwdInput = document.getElementById('admin-password-input');
    if (pwdInput) {
        const pwd = pwdInput.value.trim();
        if (pwd.length < 6) {
            showToast('Password must be at least 6 characters', true);
            return;
        }
        if (!globalConfig.admin) globalConfig.admin = {};
        globalConfig.admin.password = pwd;
    }
    return _originalSaveConfig.call(this);
};

function renderToolsTab() {
    const t = globalConfig.tools || {};
    return `
        <div class="settings-section">
            <h2 class="section-title">Document Intelligence</h2>
            
            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Enable Docs Parser</div>
                    <div class="setting-desc">Parse PDF, DOCX, XLSX, and PPTX files attached to requests. Summarizes large documents before sending to the primary model.</div>
                </div>
                <label class="switch">
                    <input type="checkbox" onchange="globalConfig.tools.docs_parser_enabled = this.checked" ${t.docs_parser_enabled ? 'checked' : ''}>
                    <span class="slider"></span>
                </label>
            </div>

            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Skip Threshold (Tokens)</div>
                    <div class="setting-desc">Documents smaller than this token count will be passed verbatim without summarization.</div>
                </div>
                <input type="number" class="input" style="width: 120px" value="${t.docs_skip_threshold || 8000}" onchange="globalConfig.tools.docs_skip_threshold = parseInt(this.value)">
            </div>

            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Summarization Model</div>
                    <div class="setting-desc">Cheap model used for context-aware document summaries. Defaults to cheapest active connection.</div>
                </div>
                <select class="input" style="width: 250px" onchange="globalConfig.tools.docs_summary_model = this.value">
                    <option value="">-- Select Model --</option>
                    ${getModelsDropdownWithCombosHTML(t.docs_summary_model || 'gpt-4o-mini')}
                </select>
            </div>
        </div>

        <div class="settings-section">
            <h2 class="section-title">Vision Bridge</h2>
            
            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Enable Vision Polyfill</div>
                    <div class="setting-desc">Automatically intercept image URLs sent to text-only models and replace them with detailed text descriptions.</div>
                </div>
                <label class="switch">
                    <input type="checkbox" onchange="globalConfig.tools.vision_bridge_enabled = this.checked" ${t.vision_bridge_enabled ? 'checked' : ''}>
                    <span class="slider"></span>
                </label>
            </div>

            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Vision Model & Max Tokens</div>
                    <div class="setting-desc">Token budget for standard vision requests.</div>
                </div>
                <div style="display:flex; gap:12px; align-items:center;">
                    <select class="input" style="width: 200px" onchange="globalConfig.tools.vision_bridge_model = this.value">
                        <option value="">-- Select Model --</option>
                        ${getModelsDropdownWithCombosHTML(t.vision_bridge_model || 'gpt-4o-mini')}
                    </select>
                    <select class="input" style="width: 100px" onchange="globalConfig.tools.vision_max_tokens = parseInt(this.value)">
                        <option value="512" ${t.vision_max_tokens === 512 ? 'selected' : ''}>512</option>
                        <option value="1024" ${(t.vision_max_tokens === 1024 || !t.vision_max_tokens) ? 'selected' : ''}>1024</option>
                        <option value="2048" ${t.vision_max_tokens === 2048 ? 'selected' : ''}>2048</option>
                        <option value="4096" ${t.vision_max_tokens === 4096 ? 'selected' : ''}>4096</option>
                    </select>
                </div>
            </div>

            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">UI/UX Design Context Override</div>
                    <div class="setting-desc">Force max_tokens to 4096 and inject exhaustive UI description prompts automatically.</div>
                </div>
                <label class="switch">
                    <input type="checkbox" onchange="globalConfig.tools.vision_ui_ux_override = this.checked" ${t.vision_ui_ux_override ? 'checked' : ''}>
                    <span class="slider"></span>
                </label>
            </div>
        </div>

        <div class="settings-section">
            <h2 class="section-title">Token Budget</h2>

            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Enable Hard Token Budget</div>
                    <div class="setting-desc">Universal max_tokens ceiling for every request. When OFF, a 65535-token floor is applied (anti-truncation). When ON, the budget below becomes a hard ceiling — requests declaring a higher max_tokens are rejected with HTTP 400 before reaching any provider.</div>
                </div>
                <label class="switch">
                    <input type="checkbox" onchange="globalConfig.tools.max_tokens_budget_enabled = this.checked" ${t.max_tokens_budget_enabled ? 'checked' : ''}>
                    <span class="slider"></span>
                </label>
            </div>

            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Budget Ceiling (Tokens)</div>
                    <div class="setting-desc">Maximum max_tokens allowed when the budget is enabled. Clamped to 1024–65535 (Qwen API hard-caps at 65535). Also caps quality-gate truncation retries.</div>
                </div>
                <input type="number" class="input" style="width: 140px" min="1024" max="65535" step="1024" value="${t.max_tokens_budget || 65535}" onchange="globalConfig.tools.max_tokens_budget = Math.max(1024, Math.min(65535, parseInt(this.value) || 65535))">
            </div>
        </div>

        <div class="settings-section">
            <h2 class="section-title">Prompt Caching & Compaction (Context Policy)</h2>
            
            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Anthropic Explicit Caching</div>
                    <div class="setting-desc">Inject <span class="badge" style="font-family:monospace;font-size:10px">cache_control: ephemeral</span> on system prompt blocks.</div>
                </div>
                <label class="switch">
                    <input type="checkbox" onchange="globalConfig.tools.caching_anthropic_explicit = this.checked" ${t.caching_anthropic_explicit ? 'checked' : ''}>
                    <span class="slider"></span>
                </label>
            </div>

            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Kimi Key-Bound Caching</div>
                    <div class="setting-desc">Inject hashed <span class="badge" style="font-family:monospace;font-size:10px">prompt_cache_key</span> on system prompt blocks for Kimi/Moonshot.</div>
                </div>
                <label class="switch">
                    <input type="checkbox" onchange="globalConfig.tools.caching_kimi_key_bound = this.checked" ${t.caching_kimi_key_bound ? 'checked' : ''}>
                    <span class="slider"></span>
                </label>
            </div>
            
            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Static-First Sorting (DeepSeek / Gemini / OpenAI)</div>
                    <div class="setting-desc">Reorder the messages array to anchor system blocks at the absolute top for implicit caching.</div>
                </div>
                <label class="switch">
                    <input type="checkbox" onchange="globalConfig.tools.caching_static_sort = this.checked" ${t.caching_static_sort ? 'checked' : ''}>
                    <span class="slider"></span>
                </label>
            </div>

            <div class="setting-row" id="cache-key-routing-row" style="flex-direction:column; align-items:stretch; gap:0;">
                <div style="display:flex; align-items:center; justify-content:space-between; width:100%;">
                    <div class="setting-info">
                        <div class="setting-title">OpenAI Cache-Key Routing (GPT-5.6)</div>
                        <div class="setting-desc">Inject a hashed <span class="badge" style="font-family:monospace;font-size:10px">prompt_cache_key</span> for GPT-5.6 Sol / Terra / Luna static system prefixes (&ge;1024 chars). Caller-supplied keys are never overwritten.</div>
                    </div>
                    <label class="switch">
                        <input type="checkbox" onchange="globalConfig.tools.caching_openai_key_bound = this.checked; scheduleAutoSave();" ${t.caching_openai_key_bound !== false ? 'checked' : ''}>
                        <span class="slider"></span>
                    </label>
                </div>
                <div style="margin-left:24px; margin-top:8px; padding-left:16px; border-left:2px solid var(--border-color);">
                    <div style="display:flex; align-items:center; justify-content:space-between; padding:10px 0; background: rgba(255, 193, 7, 0.05); border: 1px solid rgba(255, 193, 7, 0.25); border-radius:8px; padding:10px 16px;">
                        <div class="setting-info">
                            <div class="setting-title" style="font-size:13px;">↳ 24h Cache Retention <span class="badge" style="background:rgba(255,193,7,0.2);color:#b28600;font-size:10px;">Experimental</span></div>
                            <div class="setting-desc" style="font-size:12px;">Auto-inject <span class="badge" style="font-family:monospace;font-size:10px">prompt_cache_retention: 24h</span> for long-lived sessions. <strong style="color:#b28600;">May incur extra storage fees.</strong></div>
                        </div>
                        <label class="switch">
                            <input type="checkbox" onchange="globalConfig.tools.caching_openai_retention_24h = this.checked; scheduleAutoSave();" ${t.caching_openai_retention_24h ? 'checked' : ''}>
                            <span class="slider"></span>
                        </label>
                    </div>
                </div>
            </div>

            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Caching Tracker Diagnostics</div>
                    <div class="setting-desc">Emit <span class="badge" style="font-family:monospace;font-size:10px">cache_tracker</span> hit/miss strategy logs into the Logs tab for real-time cache observability.</div>
                </div>
                <label class="switch">
                    <input type="checkbox" onchange="globalConfig.tools.caching_tracker_enabled = this.checked" ${t.caching_tracker_enabled ? 'checked' : ''}>
                    <span class="slider"></span>
                </label>
            </div>

            <div class="setting-row" style="background: rgba(255, 106, 0, 0.05); border: 1px solid rgba(255, 106, 0, 0.2); padding:18px 20px; min-height:86px; gap:24px; align-items:center;">
                <div class="setting-info" style="min-width:0; padding-right:18px;">
                    <div class="setting-title" style="color: var(--danger-color); margin-bottom:6px;">&#9888; Enable Context Compaction</div>
                    <div class="setting-desc" style="line-height:1.55; max-width:760px;">DESTRUCTIVE: Aggressively trim and summarize the Dynamic Tail (older conversational turns) to save tokens on Volatile Cache providers like GLM-5.</div>
                </div>
                <div style="display:flex; flex-direction:column; align-items:flex-end; gap:10px; flex:0 0 330px; max-width:330px;">
                    <div style="display:flex; align-items:center; justify-content:flex-end; gap:12px; width:100%;">
                        <label class="switch" style="flex:0 0 auto;">
                            <input type="checkbox" onchange="globalConfig.tools.compaction_enabled = this.checked" ${t.compaction_enabled ? 'checked' : ''}>
                            <span class="slider"></span>
                        </label>
                        <select class="input" style="width: 210px; font-size:12px; padding:6px 10px;" onchange="globalConfig.tools.compaction_model = this.value">
                            <option value="">-- Compaction Model --</option>
                            ${getModelsDropdownWithCombosHTML(t.compaction_model || 'gpt-4o-mini')}
                        </select>
                    </div>
                    <div style="display:flex; align-items:center; justify-content:flex-end; gap:8px; width:100%; white-space:nowrap;">
                        <span style="font-size:11px; color:var(--text-muted);">Force compact if context &gt;</span>
                        <input type="number" min="1000" step="1000" class="input" style="width: 90px; font-size:12px; padding:6px 10px;" value="${t.compaction_threshold || 48000}" onchange="globalConfig.tools.compaction_threshold = parseInt(this.value) || 48000">
                        <span style="font-size:11px; color:var(--text-muted);">tokens</span>
                    </div>
                    <div style="display:grid; grid-template-columns: 1fr 92px; gap:8px; width:100%; align-items:center;">
                        <span style="font-size:11px; color:var(--text-muted); text-align:right;">Pin recent turns</span>
                        <input type="number" min="1" max="20" step="1" class="input" style="width: 92px; font-size:12px; padding:6px 10px;" value="${t.compaction_code_strip_turns || 3}" onchange="globalConfig.tools.compaction_code_strip_turns = Math.max(1, Math.min(20, parseInt(this.value) || 3)); this.value = globalConfig.tools.compaction_code_strip_turns">
                        <span style="font-size:11px; color:var(--text-muted); text-align:right;">Tail-trim threshold</span>
                        <input type="number" min="0" step="1000" class="input" style="width: 92px; font-size:12px; padding:6px 10px;" value="${t.compaction_tail_trim_threshold || 0}" onchange="globalConfig.tools.compaction_tail_trim_threshold = Math.max(0, parseInt(this.value) || 0); this.value = globalConfig.tools.compaction_tail_trim_threshold">
                    </div>
                </div>
            </div>
        </div>
        
        <div class="settings-section">
            <h2 class="section-title">Output Control</h2>
            
            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Dynamic Thinking Squeeze</div>
                    <div class="setting-desc">Automatically reduce <span class="badge" style="font-family:monospace;font-size:10px">budget_tokens</span> when total context approaches provider ceiling.</div>
                </div>
                <div style="display:flex; flex-direction:column; align-items:flex-end; gap:8px;">
                    <label class="switch">
                        <input type="checkbox" onchange="globalConfig.tools.output_thinking_squeeze = this.checked" ${t.output_thinking_squeeze !== false ? 'checked' : ''}>
                        <span class="slider"></span>
                    </label>
                    <div style="display:flex; align-items:center; gap:8px;">
                        <span style="font-size:11px; color:var(--text-muted);">Squeeze to:</span>
                        <input type="number" class="input" style="width: 80px; font-size:12px; padding:4px 8px;" value="${t.output_thinking_squeeze_tokens || 1024}" onchange="globalConfig.tools.output_thinking_squeeze_tokens = parseInt(this.value) || 1024">
                    </div>
                </div>
            </div>

            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Intent-Driven Output Enforcement</div>
                    <div class="setting-desc">Detect asks for JSON, tables, code, bullets, concise, or detailed output and inject a resilient format instruction into the system prompt.</div>
                </div>
                <label class="switch">
                    <input type="checkbox" onchange="globalConfig.tools.output_intent_driven = this.checked" ${t.output_intent_driven ? 'checked' : ''}>
                    <span class="slider"></span>
                </label>
            </div>
        </div>


        <div class="settings-section">
            <h2 class="section-title">Diagnostics</h2>

            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Connection Trace</div>
                    <div class="setting-desc">Log client-side socket-level details (local/remote port, TLS version) for every upstream request. Useful for diagnosing connection-pool exhaustion and keep-alive issues.</div>
                </div>
                <label class="switch">
                    <input type="checkbox" onchange="globalConfig.tools.conn_trace = this.checked" ${t.conn_trace ? 'checked' : ''}>
                    <span class="slider"></span>
                </label>
            </div>

            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Auto-Clear Logs Interval</div>
                    <div class="setting-desc">Automatically delete log entries older than this many minutes. Set to <code>0</code> to disable auto-clearing.</div>
                </div>
                <div style="display:flex; align-items:center; gap:8px;">
                    <input type="number" min="0" step="5" class="input" style="width: 80px; font-size:12px; padding:4px 8px;" value="${t.auto_clear_logs_interval || 60}" onchange="globalConfig.tools.auto_clear_logs_interval = parseInt(this.value) || 0">
                    <span style="font-size:11px; color:var(--text-muted);">minutes</span>
                </div>
            </div>
        </div>

        <div class="settings-section">
            <h2 class="section-title">Canvas <span class="badge" style="background:rgba(168,85,247,0.15);color:#a855f7;font-size:10px;">Phase 4</span></h2>

            <div class="setting-row">
                <div class="setting-info">
                    <div class="setting-title">Enable Image Generation Tool</div>
                    <div class="setting-desc">Inject a <span class="badge" style="font-family:monospace;font-size:10px">generate_image</span> tool into the request's <span class="badge" style="font-family:monospace;font-size:10px">tools</span> array, allowing text-only models to request image generation through the tool-call interface. Full execution loop arrives in a future phase.</div>
                </div>
                <label class="switch">
                    <input type="checkbox" onchange="globalConfig.tools.canvas_enabled = this.checked" ${t.canvas_enabled ? 'checked' : ''}>
                    <span class="slider"></span>
                </label>
            </div>
        </div>

        </div>
    `;
}


// --- PHASE 3 Observability JS ---

let usageDataState = [];

// Column model for the usage table. `value` is the per-row accessor used for
// both cell rendering and the per-column filter dropdowns. `filterable` controls
// whether the header shows a filter menu (time is continuous, so excluded).
let usageColsState = [
    { id: 'time',        name: 'Time',        visible: true,  filterable: false, value: r => new Date(r.timestamp).toLocaleString() },
    { id: 'provider',    name: 'Provider',    visible: true,  filterable: true,  value: r => r.provider || '—' },
    { id: 'model',       name: 'Model ID',    visible: true,  filterable: true,  value: r => r.model || '—' },
    { id: 'total_time',  name: 'Total Time',  visible: true,  filterable: true,  value: r => _fmtMs(r.total_time_ms) },
    { id: 'ttft',        name: 'TTFT',        visible: true,  filterable: true,  value: r => _fmtMs(r.ttft_ms) },
    { id: 'in_cached',   name: 'In (Cached)', visible: true,  filterable: true,  value: r => (r.in_cached || 0).toLocaleString() },
    { id: 'cache_write_tokens', name: 'Cache Write', visible: true,  filterable: true,  value: r => (r.cache_write_tokens || 0).toLocaleString() },
    { id: 'in_uncached', name: 'In (Missed)', visible: true,  filterable: true,  value: r => (r.in_uncached || 0).toLocaleString() },
    { id: 'out',         name: 'Out',         visible: true,  filterable: true,  value: r => (r.out || 0).toLocaleString() },
    { id: 'cost',        name: 'Cost',        visible: true,  filterable: true,  value: r => '$' + (r.cost || 0).toFixed(6) },
    { id: 'savings',     name: 'Saving',      visible: true,  filterable: true,  value: r => '$' + (r.savings || 0).toFixed(6) }
];

// Global (free-text) search across all columns.
let usageFilterState = '';
// Cap on how many rows renderUsageTable() paints into the DOM at once. The
// usage endpoint returns up to 500 entries per page and the full filtered set
// can be large; rendering all of them in one innerHTML is the 2-minute tab
// load. Reset to 500 whenever filters/search/timeframe change; incremented by
// 500 per "Load 500 more" click.
let usageRenderLimit = 500;

// Minimal debounce — avoids importing a lib. Used by the global search input
// so we don't re-render the whole table on every keystroke.
function _debounce(fn, ms) {
    let t = null;
    return function(...args) {
        if (t) clearTimeout(t);
        t = setTimeout(() => { t = null; fn.apply(this, args); }, ms);
    };
}
// Debounced global search handler (300ms). Sets filter state then re-renders,
// resetting the render limit so the top matches show first.
const _usageSearchDebounced = _debounce(function() { usageRenderLimit = 500; renderUsageTable(); }, 300);
function _usageSearchInput(value) {
    usageFilterState = value;
    _usageSearchDebounced();
}
// Optional custom date-time bounds applied in addition to the quick timeframe buttons.
let usageDateStartFilter = '';
let usageDateEndFilter = '';
// Analytic mode: 'token' | 'cost' | 'pricing'. Drives consumption metric + shares.
let usageViewMode = 'token';
// Timeframe: 'today' | '1D' | '7D' | '1M' | '3M' | '6M'. Default 1D (last 24h).
let usageTimeframe = '1D';
// Per-column multi-select filters: { colId: Set([value, ...]) }. Empty/missing = all pass.
let usageColFilters = {};
// Per-column dropdown search box text.
let usageColFilterSearch = {};
// Which column's dropdown is currently open (or null).
let usageOpenFilterCol = null;

// Vibrant, modern palette shared by graph nodes + donut segments.
const _USAGE_COLORS = ['#f97316','#3b82f6','#10b981','#a855f7','#ec4899','#14b8a6','#f59e0b','#6366f1','#ef4444','#06b6d4','#8b5cf6','#84cc16','#0ea5e9','#f43f5e','#eab308','#22c55e'];

// Local pricing registry — standard $/1M-token rates for well-known provider models.
// Used for the PRICING subpage display and conceptual cost reference. Missing
// models show "Unknown". NEVER fabricated from live external fetch.
const MODEL_PRICING_REGISTRY = {
    // Anthropic — Claude family (per 1M tokens)
    'claude-opus-4-1':         { in: 15.0,  out: 75.0,  cache: 1.5 },
    'claude-sonnet-5':         { in: 3.0,   out: 15.0,  cache: 0.3, cache_write: 3.75 },
    'claude-sonnet-4-5':       { in: 3.0,   out: 15.0,  cache: 0.3 },
    'claude-sonnet-4-5-20250929': { in: 3.0, out: 15.0, cache: 0.3 },
    'claude-haiku-4-5':        { in: 1.0,   out: 5.0,   cache: 0.1 },
    'claude-3-5-sonnet':       { in: 3.0,   out: 15.0,  cache: 0.3 },
    'claude-3-5-haiku':        { in: 0.8,   out: 4.0,   cache: 0.08 },
    // OpenAI
    'gpt-4o':                  { in: 2.5,   out: 10.0,  cache: 1.25 },
    'gpt-4o-mini':             { in: 0.15,  out: 0.6,   cache: 0.075 },
    'gpt-4.1':                 { in: 2.0,   out: 8.0,   cache: 0.5 },
    'gpt-4.1-mini':            { in: 0.4,   out: 1.6,   cache: 0.1 },
    'o1':                      { in: 15.0,  out: 60.0,  cache: 7.5 },
    'o3':                      { in: 10.0,  out: 40.0,  cache: 2.5 },
    'o3-mini':                 { in: 1.1,   out: 4.4,   cache: 0.55 },
    // Google — Gemini
    'gemini-2.5-pro':          { in: 1.25,  out: 10.0,  cache: 0.31 },
    'gemini-2.5-flash':        { in: 0.3,   out: 2.5,   cache: 0.075 },
    'gemini-2.0-flash':        { in: 0.1,   out: 0.4,   cache: 0.025 },
    // DeepSeek
    'deepseek-chat':           { in: 0.27,  out: 1.1,   cache: 0.027 },
    'deepseek-reasoner':       { in: 0.55,  out: 2.19,  cache: 0.055 },
    // GLM (Zhipu)
    'glm-4.6':                 { in: 0.6,   out: 2.2,   cache: 0.06 },
    'glm-4.5':                 { in: 0.6,   out: 2.2,   cache: 0.06 },
    // xAI Grok
    'grok-3':                  { in: 3.0,   out: 15.0,  cache: 0.3 },
    'grok-3-mini':             { in: 0.3,   out: 0.9,   cache: 0.03 },
    // Mistral
    'mistral-large':           { in: 2.0,   out: 6.0,   cache: 0.5 },
    'mistral-small':           { in: 0.2,   out: 0.6,   cache: 0.05 },
    // Moonshot (Kimi)
    'kimi-k3':                 { in: 3.0,   out: 15.0,  cache: 0.3 },
    'kimi-k2.7-code':          { in: 0.95,  out: 4.0,   cache: 0.19 },
    'kimi-k2.6':               { in: 0.8,   out: 4.0,   cache: 0.16 },
    'kimi-k2.5':               { in: 0.5,   out: 2.5,   cache: 0.12 },
    // Alibaba (Qwen)
    'qwen3.8-max':             { in: 2.5,   out: 3.75,  cache: 0.25 },
    'qwen3.7-max':             { in: 2.5,   out: 3.75,  cache: 0.25 },
    'qwen3.7-plus':            { in: 0.4,   out: 1.6,   cache: 0.5 },
    'qwen3.6-plus':            { in: 0.4,   out: 1.2,   cache: 0.04 },
    'qwen3.5':                 { in: 0.25,  out: 0.75,  cache: 0.025 }
};

const MODEL_PRICING_OVERRIDES_KEY = 'bsl_router_model_pricing_overrides_v1';
let modelPricingOverrides = (() => {
    try { return JSON.parse(localStorage.getItem(MODEL_PRICING_OVERRIDES_KEY) || '{}') || {}; }
    catch { return {}; }
})();

function _pricingEntry(input, output, cacheHit, cacheWrite = cacheHit) {
    return { in: input, out: output, cache_hit: cacheHit, cache_write: cacheWrite };
}

// Resolve a registry entry by exact id, normalized id, then official-family pattern.
function _lookupPricing(modelId) {
    if (!modelId) return null;
    const norm = String(modelId).toLowerCase().trim();
    const compact = norm.replace(/[^a-z0-9.]+/g, '-').replace(/^-+|-+$/g, '');
    if (modelPricingOverrides[compact]) return modelPricingOverrides[compact];
    if (modelPricingOverrides[norm]) return modelPricingOverrides[norm];
    if (MODEL_PRICING_REGISTRY[modelId]) return MODEL_PRICING_REGISTRY[modelId];
    if (MODEL_PRICING_REGISTRY[norm]) return MODEL_PRICING_REGISTRY[norm];
    if (MODEL_PRICING_REGISTRY[compact]) return MODEL_PRICING_REGISTRY[compact];

    const patternRules = [
        [/\b(claude-)?opus[-.]?4|\bopus[-.]?4/i, 'claude-opus-4-1'],
        [/\b(claude-)?sonnet[-.]?5/i, 'claude-sonnet-5'],
        [/\b(claude-)?sonnet[-.]?4/i, 'claude-sonnet-4-5'],
        [/\b(claude-)?haiku[-.]?4/i, 'claude-haiku-4-5'],
        [/\bclaude[-.]?3[-.]?5[-.]?sonnet/i, 'claude-3-5-sonnet'],
        [/\bclaude[-.]?3[-.]?5[-.]?haiku/i, 'claude-3-5-haiku'],
        [/\bgpt[-.]?4o[-.]?mini/i, 'gpt-4o-mini'],
        [/\bgpt[-.]?4o/i, 'gpt-4o'],
        [/\bgpt[-.]?4[.]?1[-.]?mini/i, 'gpt-4.1-mini'],
        [/\bgpt[-.]?4[.]?1/i, 'gpt-4.1'],
        [/\bo3[-.]?mini/i, 'o3-mini'],
        [/\bo3\b/i, 'o3'],
        [/\bo1\b/i, 'o1'],
        [/\bgemini[-.]?2[.]?5[-.]?pro/i, 'gemini-2.5-pro'],
        [/\bgemini[-.]?2[.]?5[-.]?flash/i, 'gemini-2.5-flash'],
        [/\bgemini[-.]?2[.]?0[-.]?flash/i, 'gemini-2.0-flash'],
        [/\bdeepseek[-.]?reasoner/i, 'deepseek-reasoner'],
        [/\bdeepseek[-.]?chat/i, 'deepseek-chat'],
        [/\bglm[-.]?4[.]?6/i, 'glm-4.6'],
        [/\bglm[-.]?4[.]?5/i, 'glm-4.5'],
        [/\bgrok[-.]?3[-.]?mini/i, 'grok-3-mini'],
        [/\bgrok[-.]?3/i, 'grok-3'],
        [/\bmistral[-.]?large/i, 'mistral-large'],
        [/\bmistral[-.]?small/i, 'mistral-small'],
        [/\bkimi[-.]?k3\b/i, 'kimi-k3'],
        [/\bkimi[-.]?k2[.]?7[-.]?code/i, 'kimi-k2.7-code'],
        [/\bkimi[-.]?k2[.]?6/i, 'kimi-k2.6'],
        [/\bkimi[-.]?k2[.]?5/i, 'kimi-k2.5'],
        [/\bqwen3[.]?8[-.]?max/i, 'qwen3.8-max'],
        [/\bqwen3[.]?7[-.]?max/i, 'qwen3.7-max'],
        [/\bqwen3[.]?7[-.]?plus/i, 'qwen3.7-plus'],
        [/\bqwen3[.]?6[-.]?plus/i, 'qwen3.6-plus'],
        [/\bqwen3[.]?5\b/i, 'qwen3.5']
    ];
    for (const [regex, key] of patternRules) {
        if (regex.test(compact)) return MODEL_PRICING_REGISTRY[key] || null;
    }
    for (const key of Object.keys(MODEL_PRICING_REGISTRY)) {
        if (compact === key || compact.startsWith(key) || key.startsWith(compact)) {
            return MODEL_PRICING_REGISTRY[key];
        }
    }
    return null;
}

function _pricingDisplay(price, key) {
    if (!price) return 'Unknown';
    const value = price[key]
        ?? (key === 'cache_hit' ? price.cache : undefined)
        ?? (key === 'cache_write' ? price.cache_write : undefined)
        ?? 0;
    return '$' + Number(value || 0).toFixed(3);
}

// `btn` is the clicked Save button (passed as `this`); we resolve the row via
// closest() so canonical keys containing special chars (e.g. `openai:gpt-5.5`,
// `anthropic:claude-opus-4x`, `zhipu:glm-5.2`) never depend on CSS selector
// escaping. Blank fields are normalized to 0.
function savePricingOverride(modelKey, btn) {
    const row = btn instanceof Element ? btn.closest('[data-pricing-row]') : null;
    const resolvedRow = row || document.querySelector(`[data-pricing-row="${CSS.escape(modelKey)}"]`);
    if (!resolvedRow) return;
    modelPricingOverrides[modelKey] = {
        in: Number(resolvedRow.querySelector('[data-price-field="in"]')?.value) || 0,
        out: Number(resolvedRow.querySelector('[data-price-field="out"]')?.value) || 0,
        cache_hit: Number(resolvedRow.querySelector('[data-price-field="cache_hit"]')?.value) || 0,
        cache_write: Number(resolvedRow.querySelector('[data-price-field="cache_write"]')?.value) || 0
    };
    localStorage.setItem(MODEL_PRICING_OVERRIDES_KEY, JSON.stringify(modelPricingOverrides));
    showToast('Pricing override saved');
    renderUsageTable();
}

// ── Canonical pricing registry (file-backed) ─────────────────────────────────
// Loaded from /api/pricing/registry which merges the seeded official registry
// with the offline-detected config variant mapping. One canonical row per model
// family (gpt-5.5, gpt-5.5-pro20x, gpt-5.5-pro20x-openai-compact → one row).
// null = not yet loaded; {canonical_models:{}} = loaded (possibly empty).
let canonicalPricingState = null;

async function loadCanonicalPricing(force = false) {
    if (canonicalPricingState && !force) return;
    try {
        const res = await fetch('/api/pricing/registry');
        const data = await res.json();
        canonicalPricingState = data && data.canonical_models ? data : { canonical_models: {} };
    } catch (e) {
        canonicalPricingState = { canonical_models: {}, error: String(e.message || e) };
    }
    renderUsageTable();
}

async function redetectPricing() {
    try {
        const res = await fetch('/api/pricing/detect', { method: 'POST' });
        const data = await res.json();
        canonicalPricingState = data && data.canonical_models ? data : { canonical_models: {} };
        renderUsageTable();
        showToast('Pricing re-detected');
    } catch (e) {
        showToast('Re-detect failed: ' + (e.message || e));
    }
}

// Status badge color/label per source_status.
function _pricingStatusBadge(status) {
    const map = {
        official:         { label: 'OFFICIAL',  color: '#16a34a' },
        manual:           { label: 'MANUAL',    color: '#f59e0b' },
        alias_unverified: { label: 'UNVERIFIED', color: '#f97316' },
    };
    const m = map[status] || { label: String(status || '—').toUpperCase(), color: 'var(--text-muted)' };
    return `<span style="font-size:9px;font-weight:800;letter-spacing:.06em;padding:3px 7px;border-radius:999px;background:${m.color}1f;color:${m.color};border:1px solid ${m.color}55;">${m.label}</span>`;
}

function _pricingCell(value) {
    if (value === null || value === undefined || value === '' || Number.isNaN(value)) return '—';
    return '$' + Number(value).toFixed(3);
}

function _usageColorFor(idx) {
    return _USAGE_COLORS[idx % _USAGE_COLORS.length];
}

// Derive a short uppercase badge label from a provider id / display name.
function _providerBadgeLabel(id, name) {
    const src = String(name || id || '?').replace(/[_-]+/g, ' ').trim();
    if (!src) return '?';
    const parts = src.split(/\s+/).filter(Boolean);
    if (parts.length === 1) {
        const up = parts[0].toUpperCase();
        return up.length <= 4 ? up : up.slice(0, 3);
    }
    return (parts[0][0] + parts[1][0]).toUpperCase();
}

function _fmtCost(n) { return '$' + (Number(n) || 0).toFixed(6); }
function _fmtCompact(n) {
    n = Number(n) || 0;
    if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return String(Math.round(n));
}

let logsDataState = [];
let logsFilterState = '';
let logsArtifactsState = [];
// Cap on how many console-log rows renderLogsView() paints into the DOM at
// once. Reset to 500 only when new data arrives that changes the signature
// (see _logsSignature guard in refreshLogsLive) — so the user's expanded view
// survives live polling between data changes.
let logsRenderLimit = 500;

// ── Timeframe ───────────────────────────────────────────────────────────────
const USAGE_TIMEFRAMES = ['today', '1D', '7D', '1M', '3M', '6M'];
const USAGE_TIMEFRAME_LABELS = { today: 'TODAY', '1D': '1D', '7D': '7D', '1M': '1M', '3M': '3M', '6M': '6M' };
const DAY_MS = 24 * 60 * 60 * 1000;

// Returns [start, end] Date window for the selected timeframe. Default 1D.
function _usageTimeframeRange(key) {
    const now = new Date();
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    switch (key) {
        case 'today': return [todayStart, now];
        case '1D':    return [new Date(now.getTime() - DAY_MS), now];
        case '7D':    return [new Date(now.getTime() - 7 * DAY_MS), now];
        case '1M':    return [new Date(now.getTime() - 30 * DAY_MS), now];
        case '3M':    return [new Date(now.getTime() - 90 * DAY_MS), now];
        case '6M':    return [new Date(now.getTime() - 180 * DAY_MS), now];
        default:      return [new Date(now.getTime() - DAY_MS), now];
    }
}

function _usageDateInputValue(date) {
    if (!date) return '';
    const d = date instanceof Date ? date : new Date(date);
    if (!Number.isFinite(d.getTime())) return '';
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function _usageEffectiveRange() {
    const [quickStart, quickEnd] = _usageTimeframeRange(usageTimeframe);
    const customStart = usageDateStartFilter ? new Date(usageDateStartFilter) : null;
    const customEnd = usageDateEndFilter ? new Date(usageDateEndFilter) : null;
    const start = customStart && Number.isFinite(customStart.getTime()) ? customStart : quickStart;
    const end = customEnd && Number.isFinite(customEnd.getTime()) ? customEnd : quickEnd;
    return [start, end];
}

function setUsageDateFilter(which, value) {
    if (which === 'start') usageDateStartFilter = value;
    if (which === 'end') usageDateEndFilter = value;
    usageOpenFilterCol = null;
    usageRenderLimit = 500;
    renderUsageTable();
}

function clearUsageDateFilter() {
    usageDateStartFilter = '';
    usageDateEndFilter = '';
    usageRenderLimit = 500;
    renderUsageTable();
}

// Build the x-axis bucket list for the consumption chart per timeframe.
// Each bucket: { key, label, start, end } where start/end are Date ms epochs.
function _usageBuckets(key) {
    const now = new Date();
    const buckets = [];
    if (key === 'today') {
        // 24 hourly buckets starting 00:00 local.
        const dayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        for (let h = 0; h < 24; h++) {
            const s = new Date(dayStart); s.setHours(h, 0, 0, 0);
            const e = new Date(dayStart); e.setHours(h, 59, 59, 999);
            buckets.push({ key: `h${h}`, label: `${String(h).padStart(2, '0')}:00`, start: s.getTime(), end: e.getTime() });
        }
    } else if (key === '1D') {
        // 24 hourly buckets ending at the current hour.
        const curHourStart = new Date(now); curHourStart.setMinutes(0, 0, 0);
        for (let h = 23; h >= 0; h--) {
            const s = new Date(curHourStart.getTime() - h * 60 * 60 * 1000);
            const e = new Date(s.getTime() + 60 * 60 * 1000 - 1);
            buckets.push({ key: `h${s.getHours()}`, label: `${String(s.getHours()).padStart(2, '0')}:00`, start: s.getTime(), end: e.getTime() });
        }
    } else if (key === '7D') {
        // 7 daily buckets ending current day.
        const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        for (let d = 6; d >= 0; d--) {
            const s = new Date(todayStart.getTime() - d * DAY_MS);
            const e = new Date(s.getTime() + DAY_MS - 1);
            buckets.push({ key: `d${s.toDateString()}`, label: `${s.getMonth() + 1}/${s.getDate()}`, start: s.getTime(), end: e.getTime() });
        }
    } else if (key === '1M') {
        // 30 daily buckets ending current day.
        const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        for (let d = 29; d >= 0; d--) {
            const s = new Date(todayStart.getTime() - d * DAY_MS);
            const e = new Date(s.getTime() + DAY_MS - 1);
            buckets.push({ key: `d${s.toDateString()}`, label: `${s.getMonth() + 1}/${s.getDate()}`, start: s.getTime(), end: e.getTime() });
        }
    } else {
        // 3M / 6M — grouped weekly (Mon-aligned) ending current week.
        const weeks = key === '3M' ? 13 : 26;
        // Align to Monday of current week.
        const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        const dow = (today.getDay() + 6) % 7; // 0 = Monday
        const thisMonday = new Date(today.getTime() - dow * DAY_MS);
        for (let w = weeks - 1; w >= 0; w--) {
            const s = new Date(thisMonday.getTime() - w * 7 * DAY_MS);
            const e = new Date(s.getTime() + 7 * DAY_MS - 1);
            buckets.push({ key: `w${s.toDateString()}`, label: `${s.getMonth() + 1}/${s.getDate()}`, start: s.getTime(), end: e.getTime() });
        }
    }
    return buckets;
}

function _fmtMs(ms) {
    if (ms === null || ms === undefined) return '—';
    if (ms < 1000) return `${Math.round(ms)}ms`;
    return `${(ms / 1000).toFixed(2)}s`;
}

// Metric value for a single usage row based on the active view mode.
// TOKEN mode uses input+output tokens; COST mode uses row.cost.
function _usageRowMetric(row) {
    if (usageViewMode === 'cost') return Number(row.cost) || 0;
    return (Number(row.in_cached) || 0) + (Number(row.cache_write_tokens) || 0) + (Number(row.in_uncached) || 0) + (Number(row.out) || 0);
}

function _usageMetricUnit() {
    return usageViewMode === 'cost' ? 'Cost ($)' : 'Tokens';
}

function _usageEscapeHtml(value) {
    return String(value ?? '').replace(/[&<>"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
}

function _usageJsArg(value) {
    return JSON.stringify(String(value ?? '')).replace(/</g, '\\u003c');
}

function _usageProviderName(providerId) {
    const configured = globalConfig.providers?.[providerId]?.name;
    if (configured) return configured;
    if (!providerId) return 'Unknown';
    // Prettify raw provider IDs so display names never leak the technical id
    return String(providerId)
        .replace(/[_-]+/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
        .replace(/\b\w/g, c => c.toUpperCase());
}

function _usageTokenIn(row) {
    return (Number(row.in_cached) || 0) + (Number(row.cache_write_tokens) || 0) + (Number(row.in_uncached) || 0);
}

function _usageTokenTotal(row) {
    return _usageTokenIn(row) + (Number(row.out) || 0);
}

function _usageMetricValue(row) {
    return usageViewMode === 'cost' ? (Number(row.cost) || 0) : _usageTokenTotal(row);
}

function setUsageViewMode(mode) {
    usageViewMode = mode;
    usageOpenFilterCol = null;
    usageRenderLimit = 500;
    renderUsageTable();
}

function setUsageTimeframe(key) {
    usageTimeframe = key;
    usageOpenFilterCol = null;
    usageRenderLimit = 500;
    renderUsageTable();
}

function _usageColumnValue(col, row) {
    try {
        if (col.id === 'provider') return _usageProviderName(row.provider);
        return String(col.value(row));
    }
    catch { return '—'; }
}

function _usageColumnRawValue(col, row) {
    if (col.id === 'time') return new Date(row.timestamp).toLocaleString();
    if (col.id === 'provider') return _usageProviderName(row.provider);
    if (col.id === 'model') return row.model || '—';
    return _usageColumnValue(col, row);
}

function _filterUsageData() {
    const [start, end] = _usageEffectiveRange();
    const startMs = start.getTime();
    const endMs = end.getTime();
    const filterStr = usageFilterState.trim().toLowerCase();

    return usageDataState.filter(row => {
        const rowTime = new Date(row.timestamp).getTime();
        if (!Number.isFinite(rowTime) || rowTime < startMs || rowTime > endMs) return false;

        if (filterStr) {
            const providerName = _usageProviderName(row.provider);
            const rowStr = `${Object.values(row).join(' ')} ${providerName}`.toLowerCase();
            if (!rowStr.includes(filterStr)) return false;
        }

        for (const col of usageColsState) {
            if (!Object.prototype.hasOwnProperty.call(usageColFilters, col.id)) continue;
            const selected = usageColFilters[col.id];
            const value = _usageColumnRawValue(col, row);
            if (!selected.has(value)) return false;
        }
        return true;
    });
}

function _buildProviderGraphData(data) {
    const byProvider = new Map();
    (data || []).forEach(row => {
        const id = row.provider || 'unknown';
        const rec = byProvider.get(id) || { id, name: _usageProviderName(id), requests: 0, metric: 0, latest: 0 };
        rec.requests += 1;
        rec.metric += _usageMetricValue(row);
        rec.latest = Math.max(rec.latest, new Date(row.timestamp).getTime() || 0);
        byProvider.set(id, rec);
    });
    return [...byProvider.values()].sort((a, b) => b.metric - a.metric || b.requests - a.requests);
}

// Lighten/darken a hex color by a percentage (-1..1). Used for glossy gradients.
function _usageShade(hex, pct) {
    const h = String(hex || '').replace('#', '');
    if (h.length !== 6) return hex;
    const num = parseInt(h, 16);
    let r = (num >> 16) & 0xff, g = (num >> 8) & 0xff, b = num & 0xff;
    const t = pct < 0 ? 0 : 255;
    const p = Math.abs(pct);
    r = Math.round((t - r) * p) + r;
    g = Math.round((t - g) * p) + g;
    b = Math.round((t - b) * p) + b;
    return '#' + ((1 << 24) + (r << 16) + (g << 8) + b).toString(16).slice(1);
}

function renderProviderGraphSVG(data) {
    const providers = _buildProviderGraphData(data);
    if (providers.length === 0) {
        return '<div style="color:var(--text-muted);text-align:center;padding:46px 20px;">No active providers in this timeframe.</div>';
    }

    // Current/last-used provider = most recent activity in the timeframe.
    const latestId = providers.reduce((best, p) => p.latest > (best?.latest || 0) ? p : best, providers[0]).id;
    const w = 620, h = 360, cx = w / 2, cy = h / 2;
    const nodeW = 118, nodeH = 34;
    // Keep every node fully inside the frame.
    const radius = Math.min(cx - nodeW / 2 - 8, cy - nodeH / 2 - 12);

    let gradients = '', links = '', nodes = '';
    providers.forEach((p, i) => {
        // Palette index matches the pie legend exactly (same _buildProviderGraphData ordering).
        const color = _usageColorFor(i);
        const current = p.id === latestId;
        const angle = (i / providers.length) * Math.PI * 2 - Math.PI / 2;
        const x = cx + Math.cos(angle) * radius;
        const y = cy + Math.sin(angle) * radius;
        const gid = `pgN${i}`;
        const metricLabel = usageViewMode === 'cost' ? _fmtCost(p.metric) : `${_fmtCompact(p.metric)} tokens`;
        const tip = `${_usageEscapeHtml(p.name)}${current ? ' — current/last used' : ''} — ${metricLabel} — ${p.requests || 0} request(s)`;
        gradients += `<linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="${_usageShade(color, 0.22)}"/><stop offset="100%" stop-color="${_usageShade(color, -0.16)}"/></linearGradient>`;
        links += `<path d="M ${cx.toFixed(1)} ${cy.toFixed(1)} L ${x.toFixed(1)} ${y.toFixed(1)}" fill="none" stroke="${current ? '#22c55e' : 'rgba(100,116,139,.35)'}" stroke-width="${current ? 3 : 1.3}" stroke-linecap="round" opacity="${current ? 0.95 : 0.5}"${current ? ' stroke-dasharray="7 6"' : ''}>${current ? '<animate attributeName="stroke-dashoffset" from="26" to="0" dur="1.1s" repeatCount="indefinite"/>' : ''}</path>`;
        links += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3.2" fill="#fff" stroke="${current ? '#22c55e' : 'rgba(100,116,139,.6)'}" stroke-width="2"/>`;
        nodes += `<g style="cursor:help;">
            <rect x="${(x - nodeW / 2).toFixed(1)}" y="${(y - nodeH / 2).toFixed(1)}" width="${nodeW}" height="${nodeH}" rx="9" fill="url(#${gid})" stroke="${current ? '#22c55e' : 'rgba(255,255,255,.85)'}" stroke-width="${current ? 2.6 : 1}" filter="url(#pgShadow)"/>
            <rect x="${(x - nodeW / 2).toFixed(1)}" y="${(y - nodeH / 2).toFixed(1)}" width="${nodeW}" height="${nodeH / 2}" rx="9" fill="rgba(255,255,255,.22)"/>
            <text x="${x.toFixed(1)}" y="${(y + 4).toFixed(1)}" text-anchor="middle" fill="#fff" font-size="11" font-weight="900">${_usageEscapeHtml(p.name).slice(0, 18)}</text>
            <title>${tip}</title>
        </g>`;
    });

    return `<svg viewBox="0 0 ${w} ${h}" aria-label="Provider graph — providers used, linked to current/last used" style="width:100%;height:100%;min-height:340px;display:block;">
        <defs>
            <filter id="pgShadow" x="-30%" y="-60%" width="160%" height="220%"><feDropShadow dx="0" dy="5" stdDeviation="6" flood-color="#0f172a" flood-opacity="0.16"/></filter>
            <radialGradient id="pgCleanBg" cx="50%" cy="42%"><stop offset="0%" stop-color="#ffffff"/><stop offset="64%" stop-color="#f8fafc"/><stop offset="100%" stop-color="#eef2f7"/></radialGradient>
            <radialGradient id="bslNode" cx="42%" cy="35%"><stop offset="0%" stop-color="#fff7ed"/><stop offset="56%" stop-color="#fb923c"/><stop offset="100%" stop-color="#ea580c"/></radialGradient>
            ${gradients}
        </defs>
        <rect width="${w}" height="${h}" rx="16" fill="url(#pgCleanBg)"/>
        <circle cx="${cx}" cy="${cy}" r="${radius}" fill="none" stroke="rgba(15,23,42,.05)" stroke-width="1"/>
        ${links}
        <g filter="url(#pgShadow)">
            <circle cx="${cx}" cy="${cy}" r="40" fill="url(#bslNode)" stroke="#fff7ed" stroke-width="3"/>
            <text x="${cx}" y="${cy - 3}" text-anchor="middle" fill="#fff" font-size="14" font-weight="900">BSL</text>
            <text x="${cx}" y="${cy + 12}" text-anchor="middle" fill="#fff7ed" font-size="7.5" font-weight="900">ROUTER</text>
        </g>
        ${nodes}
        <text x="16" y="${h - 14}" font-size="10" font-weight="800" style="fill:var(--text-muted);"><tspan style="fill:#22c55e;">●</tspan> current / last used</text>
    </svg>`;
}

function renderProviderSharePie(data) {
    const providers = _buildProviderGraphData(data)
        .map((p, i) => ({ ...p, val: p.metric || p.requests || 0, color: _usageColorFor(i) }))
        .sort((a, b) => b.val - a.val);
    if (providers.length === 0) return '<div style="color:var(--text-muted);padding:40px 12px;text-align:center;">No provider share yet.</div>';

    const total = providers.reduce((s, p) => s + p.val, 0);
    if (total <= 0) {
        return '<div style="color:var(--text-muted);padding:40px 12px;text-align:center;">No provider share yet.</div>';
    }

    const topProviders = providers.slice(0, 9);
    const overflow = providers.slice(9);
    if (overflow.length) {
        topProviders.push({
            id: 'other',
            name: `Other (${overflow.length})`,
            requests: overflow.reduce((s, p) => s + (p.requests || 0), 0),
            metric: overflow.reduce((s, p) => s + (p.metric || 0), 0),
            val: overflow.reduce((s, p) => s + p.val, 0),
            color: '#94a3b8'
        });
    }

    // Exploded variable-radius donut:
    // - Angular sweep is the ONLY data channel for share.
    // - Outer radius is decorative: a stable, non-monotonic spike pattern.
    // - This avoids both misleading mappings: biggest share => longest radius
    //   AND biggest share => shortest radius. It should read as irregular.
    const cx = 190, cy = 178, innerR = 56, baseOuterR = 74;
    const _stableHash01 = (str) => {
        let h = 2166136261;
        for (let k = 0; k < str.length; k++) { h ^= str.charCodeAt(k); h = Math.imul(h, 16777619); }
        return ((h >>> 0) % 10007) / 10007;
    };
    const _clamp01 = (v) => Math.max(0, Math.min(1, v));
    // Deliberately non-monotonic by displayed slice position; provider hash only
    // adds small stable jitter so the result feels organic without polling twitch.
    const spikeProfile = [0.64, 0.92, 0.38, 0.78, 0.20, 0.86, 0.48, 0.72, 0.30, 0.58];
    let segs = '', gradients = '', insideLabels = '';
    const radialLabels = [];
    let cursor = 0;

    const point = (x, y, r, pct) => {
        const a = (-90 + (pct / 100) * 360) * Math.PI / 180;
        return { x: x + Math.cos(a) * r, y: y + Math.sin(a) * r, a };
    };
    const donutArcPath = (x, y, rOuter, rInner, startPct, endPct) => {
        const span = Math.max(0.001, endPct - startPct);
        const large = span > 50 ? 1 : 0;
        const os = point(x, y, rOuter, startPct);
        const oe = point(x, y, rOuter, endPct);
        const is = point(x, y, rInner, startPct);
        const ie = point(x, y, rInner, endPct);
        return `M ${os.x.toFixed(2)} ${os.y.toFixed(2)} A ${rOuter.toFixed(2)} ${rOuter.toFixed(2)} 0 ${large} 1 ${oe.x.toFixed(2)} ${oe.y.toFixed(2)} L ${ie.x.toFixed(2)} ${ie.y.toFixed(2)} A ${rInner.toFixed(2)} ${rInner.toFixed(2)} 0 ${large} 0 ${is.x.toFixed(2)} ${is.y.toFixed(2)} Z`;
    };

    topProviders.forEach((p, i) => {
        const pct = (p.val / total) * 100;
        const start = cursor;
        const end = cursor + pct;
        const mid = start + pct / 2;
        cursor = end;
        const gid = `psSeg${i}`;
        const metricLabel = usageViewMode === 'cost' ? _fmtCost(p.val) : `${_fmtCompact(p.val)} tokens`;
        const tip = `${_usageEscapeHtml(p.name)} — ${pct.toFixed(2)}% — ${metricLabel} — ${p.requests || 0} request(s)`;
        const rad = (-90 + (mid / 100) * 360) * Math.PI / 180;
        // Radius is decorative, not data: non-monotonic stable profile + tiny jitter.
        const jitter = (_stableHash01(String(p.id || p.name || i)) - 0.5) * 0.16;
        const radNorm = _clamp01(spikeProfile[i % spikeProfile.length] + jitter);
        const outerR = baseOuterR + 12 + radNorm * 64;   // ~86..150px, share-independent
        const sx = cx;
        const sy = cy;
        gradients += `<linearGradient id="${gid}" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="${_usageShade(p.color, 0.22)}"/><stop offset="100%" stop-color="${_usageShade(p.color, -0.16)}"/></linearGradient>`;
        segs += `<path class="psw-seg" d="${donutArcPath(sx, sy, outerR, innerR, start, end)}" fill="url(#${gid})"><title>${tip}</title></path>`;

        if (pct >= 8) {
            const labelR = innerR + (outerR - innerR) * 0.58;
            const lp = point(sx, sy, labelR, mid);
            const arcLen = Math.max(1, ((end - start) / 100) * 2 * Math.PI * labelR);
            const ringWidth = Math.max(1, outerR - innerR);
            const labelChars = `${pct.toFixed(0)}%`.length;
            const labelFont = Math.max(11, Math.min(21, Math.min(ringWidth * 0.42, arcLen / (labelChars * 0.58))));
            insideLabels += `<text class="psw-in" x="${lp.x.toFixed(1)}" y="${(lp.y + labelFont * 0.28).toFixed(1)}" text-anchor="middle" style="font-size:${labelFont.toFixed(1)}px;stroke-width:${Math.max(2, labelFont * 0.14).toFixed(1)}px;">${pct.toFixed(0)}%<title>${tip}</title></text>`;
        } else if (pct >= 0.4) {
            const anchor = point(cx, cy, outerR + 3, mid);
            const labelR = outerR + 34 + (i % 3) * 7;
            const label = point(cx, cy, labelR, mid);
            const cos = Math.cos(rad);
            const sin = Math.sin(rad);
            let anchorMode = 'middle';
            if (cos > 0.28) anchorMode = 'start';
            else if (cos < -0.28) anchorMode = 'end';
            radialLabels.push({
                ax: anchor.x, ay: anchor.y,
                lx: label.x + (anchorMode === 'start' ? 7 : anchorMode === 'end' ? -7 : 0),
                ly: label.y + (Math.abs(cos) < 0.28 ? (sin >= 0 ? 12 : -6) : 4),
                mid, rad, pct, tip, anchorMode,
                elbowX: point(cx, cy, outerR + 18, mid).x,
                elbowY: point(cx, cy, outerR + 18, mid).y,
            });
        }
    });

    // Nudge nearby perimeter labels apart without forcing them into left/right columns.
    radialLabels.sort((a, b) => a.ly - b.ly);
    const labelGap = 15;
    for (let i = 1; i < radialLabels.length; i++) {
        if (Math.abs(radialLabels[i].lx - radialLabels[i - 1].lx) < 52 && radialLabels[i].ly < radialLabels[i - 1].ly + labelGap) {
            radialLabels[i].ly = radialLabels[i - 1].ly + labelGap;
        }
    }
    let wiredEls = radialLabels.map(w => {
        const labelY = Math.max(14, Math.min(342, w.ly));
        return `<path class="psw-wire" d="M ${w.ax.toFixed(1)} ${w.ay.toFixed(1)} L ${w.elbowX.toFixed(1)} ${w.elbowY.toFixed(1)} L ${w.lx.toFixed(1)} ${labelY.toFixed(1)}"/>` +
            `<circle class="psw-dot" cx="${w.ax.toFixed(1)}" cy="${w.ay.toFixed(1)}" r="3"/>` +
            `<text class="psw-out" x="${w.lx.toFixed(1)}" y="${(labelY + 4).toFixed(1)}" text-anchor="${w.anchorMode}">${w.pct.toFixed(w.pct < 1 ? 1 : 0)}%<title>${w.tip}</title></text>`;
    }).join('');

    const topP = topProviders[0];

    const legend = topProviders.map((p) => {
        const pct = (p.val / total) * 100;
        const metricLabel = usageViewMode === 'cost' ? _fmtCost(p.val) : `${_fmtCompact(p.val)} tokens`;
        const tooltip = `${_usageEscapeHtml(p.name)} — ${pct.toFixed(2)}% — ${metricLabel} — ${p.requests || 0} request(s)`;
        return `<div title="${tooltip}" style="display:flex;align-items:center;gap:10px;min-width:0;padding:7px 8px;border-radius:10px;cursor:help;">
            <span style="width:9px;height:9px;flex:0 0 9px;border-radius:999px;background:${p.color};box-shadow:0 0 0 3px ${p.color}22;"></span>
            <span style="font-size:12px;font-weight:850;color:var(--text-main);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${_usageEscapeHtml(p.name)}</span>
        </div>`;
    }).join('');

    return `<div style="width:100%;height:100%;display:grid;grid-template-columns:minmax(278px,1fr) minmax(150px,180px);gap:12px;align-items:center;">
        <style>
            .psw-seg { cursor: help; filter: drop-shadow(0 12px 18px rgba(15,23,42,.15)); transition: transform .22s ease, filter .22s ease, opacity .22s ease; transform-box: fill-box; transform-origin: center; }
            .psw-seg:hover { transform: scale(1.035); filter: drop-shadow(0 18px 24px rgba(15,23,42,.22)) brightness(1.06) saturate(1.08); }
            .psw-in { font-size: 21px; font-weight: 900; letter-spacing: -.04em; fill: #fff; paint-order: stroke; stroke: rgba(15,23,42,.16); stroke-width: 3px; stroke-linejoin: round; cursor: help; }
            .psw-out { font-size: 15px; font-weight: 900; fill: var(--text-main); paint-order: stroke; stroke: var(--surface-color); stroke-width: 4px; stroke-linejoin: round; cursor: help; }
            .psw-wire { fill: none; stroke: rgba(100,116,139,.58); stroke-width: 1.35; }
            .psw-dot { fill: var(--surface-color); stroke: rgba(15,23,42,.5); stroke-width: 2; }
        </style>
        <div style="display:flex;align-items:center;justify-content:center;min-width:0;">
            <svg viewBox="0 0 380 356" aria-label="Provider share exploded variable-radius chart" style="width:100%;max-width:380px;height:100%;min-height:356px;overflow:visible;">
                <defs>
                    <radialGradient id="pswCenter"><stop offset="0%" stop-color="#ffffff"/><stop offset="62%" stop-color="#f8fafc"/><stop offset="100%" stop-color="#e2e8f0"/></radialGradient>
                    ${gradients}
                </defs>
                <circle cx="${cx}" cy="${cy}" r="${innerR + 7}" fill="#fff" filter="drop-shadow(0 10px 18px rgba(15,23,42,.14))"/>
                ${segs}
                <circle cx="${cx}" cy="${cy}" r="${innerR - 10}" fill="url(#pswCenter)" stroke="rgba(15,23,42,.08)" stroke-width="1"/>
                <text x="${cx}" y="${cy - 7}" text-anchor="middle" style="font-size:9px;font-weight:800;letter-spacing:.12em;fill:var(--text-muted);text-transform:uppercase;">Top</text>
                <text x="${cx}" y="${cy + 13}" text-anchor="middle" style="font-size:15px;font-weight:900;letter-spacing:-.02em;fill:var(--text-main);">${_usageEscapeHtml(topP.name).slice(0, 14)}</text>
                ${insideLabels}
                ${wiredEls}
            </svg>
        </div>
        <div style="height:100%;min-height:340px;display:flex;flex-direction:column;justify-content:center;border-left:1px solid var(--border-color);padding-left:12px;min-width:0;">
            <div style="font-size:10px;color:var(--text-muted);font-weight:900;text-transform:uppercase;letter-spacing:.08em;margin-bottom:8px;">Provider</div>
            <div style="display:flex;flex-direction:column;gap:2px;min-width:0;">${legend}</div>
        </div>
    </div>`;
}

function renderConsumptionChart(data) {
    const buckets = _usageBuckets(usageTimeframe);
    const modelTotals = new Map();
    const bucketData = buckets.map(b => ({ ...b, models: new Map(), total: 0 }));
    data.forEach(row => {
        const ts = new Date(row.timestamp).getTime();
        const bucket = bucketData.find(b => ts >= b.start && ts <= b.end);
        if (!bucket) return;
        const model = row.model || 'unknown';
        const val = _usageMetricValue(row);
        bucket.models.set(model, (bucket.models.get(model) || 0) + val);
        bucket.total += val;
        modelTotals.set(model, (modelTotals.get(model) || 0) + val);
    });
    const models = [...modelTotals.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8).map(([m]) => m);
    const w = 920, h = 310, pad = { top: 24, right: 22, bottom: 54, left: 62 };
    const innerW = w - pad.left - pad.right, innerH = h - pad.top - pad.bottom;
    const maxVal = Math.max(1, ...bucketData.map(b => b.total));
    const barStep = innerW / Math.max(1, bucketData.length);
    const barW = Math.max(4, Math.min(24, barStep * 0.64));
    let grid = '', bars = '', labels = '';
    for (let i = 0; i <= 4; i++) {
        const y = pad.top + innerH - (innerH * i / 4);
        const val = maxVal * i / 4;
        grid += `<line x1="${pad.left}" y1="${y}" x2="${w - pad.right}" y2="${y}" stroke="var(--border-color)" opacity="0.45"/>`;
        grid += `<text x="${pad.left - 8}" y="${y + 4}" text-anchor="end" font-size="10" fill="var(--text-muted)">${usageViewMode === 'cost' ? '$' + val.toFixed(val < 1 ? 2 : 0) : _fmtCompact(val)}</text>`;
    }
    bucketData.forEach((bucket, bi) => {
        const x = pad.left + bi * barStep + (barStep - barW) / 2;
        let y = pad.top + innerH;
        models.forEach((model, mi) => {
            const val = bucket.models.get(model) || 0;
            if (val <= 0) return;
            const bh = Math.max(1, (val / maxVal) * innerH);
            y -= bh;
            bars += `<rect x="${x}" y="${y}" width="${barW}" height="${bh}" rx="${barW > 10 ? 4 : 2}" fill="${_usageColorFor(mi)}" opacity="0.92"><title>${_usageEscapeHtml(model)} — ${usageViewMode === 'cost' ? _fmtCost(val) : _fmtCompact(val) + ' tokens'}</title></rect>`;
        });
        const showLabel = bucketData.length <= 30 || bi % Math.ceil(bucketData.length / 14) === 0;
        if (showLabel) labels += `<text x="${x + barW / 2}" y="${h - 28}" text-anchor="middle" font-size="9" fill="var(--text-muted)" transform="rotate(-35 ${x + barW / 2} ${h - 28})">${bucket.label}</text>`;
    });
    const legend = models.map((m, i) => `<span style="display:inline-flex;align-items:center;gap:6px;margin:6px 12px 0 0;font-size:11px;color:var(--text-muted);"><span style="width:10px;height:10px;border-radius:3px;background:${_usageColorFor(i)};"></span>${_usageEscapeHtml(m)}</span>`).join('');
    return `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;"><div style="font-size:12px;font-weight:800;color:var(--text-main);">Model consumption share • ${_usageMetricUnit()}</div><div style="font-size:11px;color:var(--text-muted);">${USAGE_TIMEFRAME_LABELS[usageTimeframe]}</div></div>
        <svg viewBox="0 0 ${w} ${h}" style="width:100%;height:auto;display:block;">${grid}${bars}${labels}</svg>
        <div style="display:flex;flex-wrap:wrap;margin-top:6px;">${legend || '<span style="font-size:12px;color:var(--text-muted);">No model consumption in this timeframe.</span>'}</div>`;
}

function renderPricingPage() {
    // Loading guard: if canonical data isn't fetched yet, show a placeholder and
    // kick off the load (it calls renderUsageTable again when done).
    if (!canonicalPricingState) {
        loadCanonicalPricing();
        return `<div class="settings-section"><div style="background:var(--surface-color);border:1px solid var(--border-color);border-radius:18px;padding:32px;text-align:center;color:var(--text-muted);font-size:13px;">Loading canonical pricing registry…</div></div>`;
    }
    if (canonicalPricingState.error) {
        return `<div class="settings-section"><div style="background:var(--surface-color);border:1px solid var(--border-color);border-radius:18px;padding:18px;color:var(--danger-color);">Failed to load pricing registry: ${_usageEscapeHtml(canonicalPricingState.error)}</div></div>`;
    }

    const families = Object.entries(canonicalPricingState.canonical_models || {})
        .sort((a, b) => String(a[1].provider || '').localeCompare(String(b[1].provider || '')) || String(a[1].display_name || a[0]).localeCompare(String(b[1].display_name || b[0])));

    let rows = '';
    for (const [key, f] of families) {
        const override = modelPricingOverrides[key] || null;
        const inVal = override ? override.in : f.input_per_1m;
        const outVal = override ? override.out : f.output_per_1m;
        const chVal = override ? override.cache_hit : f.cache_hit_per_1m;
        const cwVal = override ? override.cache_write : f.cache_write_per_1m;
        const rowStatus = override ? 'manual' : f.source_status;
        const sourceUrl = f.source_url ? ` <a href="${_usageEscapeHtml(f.source_url)}" target="_blank" rel="noopener" style="font-size:10px;color:var(--brand-color);text-decoration:none;">source ↗</a>` : '';

        rows += `<tr data-pricing-row="${_usageEscapeHtml(key)}" style="border-bottom:1px solid var(--border-color);vertical-align:top;">
            <td style="padding:10px 12px;">
                <div style="font-size:13px;font-weight:800;color:var(--text-main);">${_usageEscapeHtml(f.display_name || key)}</div>
                <div style="font-family:monospace;font-size:10px;color:var(--text-muted);margin-top:2px;">${_usageEscapeHtml(key)}</div>
            </td>
            <td style="padding:10px 12px;font-size:12px;">${_usageEscapeHtml(f.provider || '—')}</td>
            <td style="padding:10px 12px;"><input data-price-field="in" class="input" type="number" step="0.001" value="${Number(inVal ?? 0).toFixed(3)}" style="width:84px;font-size:12px;padding:5px 7px;"></td>
            <td style="padding:10px 12px;"><input data-price-field="out" class="input" type="number" step="0.001" value="${Number(outVal ?? 0).toFixed(3)}" style="width:84px;font-size:12px;padding:5px 7px;"></td>
            <td style="padding:10px 12px;"><input data-price-field="cache_hit" class="input" type="number" step="0.001" value="${Number(chVal ?? 0).toFixed(3)}" style="width:84px;font-size:12px;padding:5px 7px;"></td>
            <td style="padding:10px 12px;"><input data-price-field="cache_write" class="input" type="number" step="0.001" value="${Number(cwVal ?? 0).toFixed(3)}" style="width:84px;font-size:12px;padding:5px 7px;"></td>
            <td style="padding:10px 12px;">${_pricingStatusBadge(rowStatus)}${sourceUrl}</td>
            <td style="padding:10px 12px;text-align:right;">
                <button class="btn btn-outline" style="padding:5px 10px;font-size:11px;font-weight:800;" onclick="savePricingOverride('${_usageEscapeHtml(key)}')">Save</button>
            </td>
        </tr>`;
    }

    const officialCount = families.filter(([, f]) => f.source_status === 'official').length;
    const subtitle = `${families.length} canonical famil${families.length === 1 ? 'y' : 'ies'} · ${officialCount} official · vendor/future aliases marked UNVERIFIED`;

    return `<div class="settings-section"><div style="background:var(--surface-color);border:1px solid var(--border-color);border-radius:18px;padding:18px;box-shadow:0 16px 40px rgba(15,23,42,.06);">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:14px;flex-wrap:wrap;">
            <div>
                <h2 class="section-title" style="margin:0 0 4px 0;">Canonical Model Pricing</h2>
                <div style="font-size:12px;color:var(--text-muted);">${_usageEscapeHtml(subtitle)}. One row per model version. No prices are fabricated; unverified families show —.</div>
            </div>
            <div style="display:flex;gap:8px;">
                <button class="btn btn-outline" style="font-size:11px;font-weight:800;" onclick="redetectPricing()">Re-detect</button>
                <button class="btn btn-outline" onclick="setUsageViewMode('token')">Back to analytics</button>
            </div>
        </div>
        <table class="providers-table" style="width:100%;border-collapse:collapse;font-size:13px;">
            <thead><tr style="border-bottom:1px solid var(--border-color);text-align:left;">
                <th style="padding:10px 12px;">Canonical Model</th>
                <th style="padding:10px 12px;">Provider</th>
                <th style="padding:10px 12px;">Input / 1M</th>
                <th style="padding:10px 12px;">Output / 1M</th>
                <th style="padding:10px 12px;">Cache Hit / 1M</th>
                <th style="padding:10px 12px;">Cache Write / 1M</th>
                <th style="padding:10px 12px;">Source</th>
                <th style="padding:10px 12px;text-align:right;">Actions</th>
            </tr></thead>
            <tbody>${rows || '<tr><td colspan="8" style="padding:22px;text-align:center;color:var(--text-muted);">No canonical families detected. Click Re-detect.</td></tr>'}</tbody>
        </table>
    </div></div>`;
}

async function loadUsageData() {
    try {
        // Probe total count, then fetch the newest 2000 entries.
        // API returns oldest-first (append-only list), so we offset from
        // the end and reverse to get newest-first for the UI.
        const probeRes = await fetch('/api/observability/usage?limit=1&offset=0');
        const probeData = await probeRes.json();
        const total = (probeData && probeData.total) ? probeData.total : 0;
        const fetchOffset = Math.max(0, total - 2000);
        const res = await fetch(`/api/observability/usage?limit=2000&offset=${fetchOffset}`);
        const data = await res.json();
        const entries = Array.isArray(data) ? data : (data && data.entries ? data.entries : []);
        // Reverse oldest-first → newest-first for display.
        usageDataState = entries.slice().reverse();
        usageRenderLimit = 500;
        renderUsageTable();
    } catch (e) {
        const container = document.getElementById('usage-container');
        if (container) container.innerHTML = `<div class="empty-state" style="color:var(--danger-color)">Error loading usage data: ${_usageEscapeHtml(e.message)}</div>`;
    }
}

// "Load 500 more" handler for the usage table — grows usageRenderLimit by 500.
function loadMoreUsageRows() {
    usageRenderLimit += 500;
    renderUsageTable();
}

function toggleUsageCol(idx) {
    usageColsState[idx].visible = !usageColsState[idx].visible;
    usageRenderLimit = 500;
    renderUsageTable();
}

function toggleUsageColumnFilter(colId) {
    usageOpenFilterCol = usageOpenFilterCol === colId ? null : colId;
    renderUsageTable();
}

function setUsageColumnFilterSearch(colId, value) {
    usageColFilterSearch[colId] = value;
    renderUsageTable();
}

function _usageColumnUniverse(col, data) {
    return [...new Set(data.map(row => _usageColumnRawValue(col, row)))].sort((a, b) => String(a).localeCompare(String(b)));
}

function selectAllUsageColumn(colId) {
    delete usageColFilters[colId];
    renderUsageTable();
}

function unselectAllUsageColumn(colId) {
    usageColFilters[colId] = new Set();
    renderUsageTable();
}

function toggleUsageColumnValue(colId, value) {
    const col = usageColsState.find(c => c.id === colId);
    const universe = _usageColumnUniverse(col, usageDataState);
    const current = Object.prototype.hasOwnProperty.call(usageColFilters, colId) ? new Set(usageColFilters[colId]) : new Set(universe);
    const selected = current;
    if (selected.has(value)) selected.delete(value); else selected.add(value);
    if (selected.size === universe.length) delete usageColFilters[colId];
    else usageColFilters[colId] = selected;
    usageRenderLimit = 500;
    renderUsageTable();
}

function renderUsageColumnMenu(col, data) {
    if (usageOpenFilterCol !== col.id) return '';
    const universe = _usageColumnUniverse(col, data);
    const search = usageColFilterSearch[col.id] || '';
    const selected = usageColFilters[col.id] || new Set();
    const visibleValues = universe.filter(v => String(v).toLowerCase().includes(search.toLowerCase())).slice(0, 250);
    const items = visibleValues.map(value => {
        const checked = !Object.prototype.hasOwnProperty.call(usageColFilters, col.id) || selected.has(value);
        return `<label style="display:flex;align-items:center;gap:8px;padding:5px 2px;font-size:12px;cursor:pointer;"><input type="checkbox" ${checked ? 'checked' : ''} onchange="toggleUsageColumnValue('${col.id}', ${_usageJsArg(value)})"><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${_usageEscapeHtml(value)}</span></label>`;
    }).join('');
    return `<div style="position:absolute;top:28px;left:0;z-index:80;width:280px;max-height:380px;overflow:auto;background:#ffffff;color:var(--text-main);border:1px solid var(--border-color);border-radius:12px;padding:10px;box-shadow:0 18px 45px rgba(15,23,42,.24);backdrop-filter:none;">
        <input class="input" value="${_usageEscapeHtml(search)}" placeholder="Search ${_usageEscapeHtml(col.name)}" oninput="setUsageColumnFilterSearch('${col.id}', this.value)" style="width:100%;font-size:12px;padding:6px 8px;margin-bottom:8px;background:#ffffff;">
        <div style="display:flex;gap:6px;margin-bottom:8px;"><button class="btn btn-outline" style="font-size:11px;padding:3px 7px;background:#ffffff;" onclick="selectAllUsageColumn('${col.id}')">Select all</button><button class="btn btn-outline" style="font-size:11px;padding:3px 7px;background:#ffffff;" onclick="unselectAllUsageColumn('${col.id}')">Unselect all</button></div>
        <div style="background:#ffffff;">${items || '<div style="padding:10px;color:var(--text-muted);font-size:12px;">No values</div>'}</div>
    </div>`;
}

function renderUsageTable() {
    const container = document.getElementById('usage-container');
    if (!container) return;

    const filteredData = _filterUsageData();
    const totalRequests = filteredData.length;
    const totalInput = filteredData.reduce((s, r) => s + _usageTokenIn(r), 0);
    const totalOutput = filteredData.reduce((s, r) => s + (Number(r.out) || 0), 0);
    const totalCost = filteredData.reduce((s, r) => s + (Number(r.cost) || 0), 0);
    const [effectiveStart, effectiveEnd] = _usageEffectiveRange();

    const modeButtons = ['token', 'cost', 'pricing'].map(mode => `<button class="btn ${usageViewMode === mode ? 'btn-primary' : 'btn-outline'}" style="padding:7px 14px;font-size:12px;font-weight:800;letter-spacing:.04em;" onclick="setUsageViewMode('${mode}')">${mode.toUpperCase()}</button>`).join('');
    const timeframeButtons = USAGE_TIMEFRAMES.map(key => `<button class="btn ${usageTimeframe === key ? 'btn-primary' : 'btn-outline'}" style="padding:7px 12px;font-size:12px;font-weight:800;" onclick="setUsageTimeframe('${key}')">${USAGE_TIMEFRAME_LABELS[key]}</button>`).join('');

    let html = `<div class="settings-section">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:16px;flex-wrap:wrap;">
            <div style="display:flex;gap:8px;align-items:center;">${modeButtons}</div>
            <div style="display:flex;gap:6px;align-items:center;">${timeframeButtons}<button class="btn btn-outline" onclick="loadUsageData()" style="padding:7px 10px;font-size:12px;">Refresh</button></div>
        </div>`;

    if (usageViewMode === 'pricing') {
        container.innerHTML = html + renderPricingPage() + '</div>';
        return;
    }

    html += `<div style="display:grid;grid-template-columns:repeat(4,minmax(150px,1fr));gap:14px;margin-bottom:18px;">
        <div style="background:var(--surface-color);border:1px solid var(--border-color);border-radius:16px;padding:16px;box-shadow:0 12px 30px rgba(15,23,42,.05);"><div style="font-size:11px;font-weight:800;color:var(--text-muted);">TOTAL REQUESTS</div><div style="font-size:25px;font-weight:900;">${totalRequests.toLocaleString()}</div></div>
        <div style="background:var(--surface-color);border:1px solid var(--border-color);border-radius:16px;padding:16px;box-shadow:0 12px 30px rgba(15,23,42,.05);"><div style="font-size:11px;font-weight:800;color:var(--text-muted);">TOTAL INPUT TOKENS</div><div style="font-size:25px;font-weight:900;color:#f97316;">${totalInput.toLocaleString()}</div></div>
        <div style="background:var(--surface-color);border:1px solid var(--border-color);border-radius:16px;padding:16px;box-shadow:0 12px 30px rgba(15,23,42,.05);"><div style="font-size:11px;font-weight:800;color:var(--text-muted);">OUTPUT TOKENS</div><div style="font-size:25px;font-weight:900;color:#10b981;">${totalOutput.toLocaleString()}</div></div>
        <div style="background:var(--surface-color);border:1px solid var(--border-color);border-radius:16px;padding:16px;box-shadow:0 12px 30px rgba(15,23,42,.05);"><div style="font-size:11px;font-weight:800;color:var(--text-muted);">EST. COST</div><div style="font-size:25px;font-weight:900;color:#f59e0b;">${_fmtCost(totalCost)}</div></div>
    </div>

    <div style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;margin-bottom:18px;align-items:stretch;">
        <div style="background:var(--surface-color);border:1px solid var(--border-color);border-radius:18px;padding:16px;box-shadow:0 16px 40px rgba(15,23,42,.06);height:430px;display:flex;flex-direction:column;min-width:0;"><div style="font-size:13px;font-weight:900;margin-bottom:10px;">Provider Graph</div><div style="flex:1;min-height:0;">${renderProviderGraphSVG(filteredData)}</div></div>
        <div style="background:var(--surface-color);border:1px solid var(--border-color);border-radius:18px;padding:16px;box-shadow:0 16px 40px rgba(15,23,42,.06);height:430px;display:flex;flex-direction:column;min-width:0;"><div style="font-size:13px;font-weight:900;margin-bottom:10px;text-align:center;">Provider Share</div><div style="flex:1;min-height:0;display:flex;align-items:stretch;justify-content:center;">${renderProviderSharePie(filteredData)}</div></div>
    </div>

    <div style="background:var(--surface-color);border:1px solid var(--border-color);border-radius:18px;padding:16px;box-shadow:0 16px 40px rgba(15,23,42,.06);margin-bottom:18px;">${renderConsumptionChart(filteredData)}</div>

    <div style="background:var(--surface-color);border:1px solid var(--border-color);border-radius:18px;padding:16px;box-shadow:0 16px 40px rgba(15,23,42,.06);">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap;">
            <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
                <h2 class="section-title" style="margin:0;">Usage Data</h2>
                <input type="datetime-local" class="input" value="${_usageEscapeHtml(_usageDateInputValue(effectiveStart))}" onchange="setUsageDateFilter('start', this.value)" style="width:185px;font-size:12px;padding:6px 8px;">
                <span style="font-size:12px;color:var(--text-muted);font-weight:800;">to</span>
                <input type="datetime-local" class="input" value="${_usageEscapeHtml(_usageDateInputValue(effectiveEnd))}" onchange="setUsageDateFilter('end', this.value)" style="width:185px;font-size:12px;padding:6px 8px;">
                <button class="btn btn-outline" onclick="clearUsageDateFilter()" style="padding:5px 9px;font-size:11px;">Clear dates</button>
            </div>
            <input type="text" class="input" placeholder="Global search" value="${_usageEscapeHtml(usageFilterState)}" oninput="_usageSearchInput(this.value)" style="width:240px;font-size:12px;">
        </div>
        <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:10px;min-height:22px;">
            ${usageColsState.map((col, idx) => !col.visible ? `<button class="btn btn-outline" onclick="toggleUsageCol(${idx})" style="padding:3px 8px;font-size:11px;border-style:dashed;">+ ${_usageEscapeHtml(col.name)}</button>` : '').join('')}
        </div>
        <table class="providers-table" style="width:100%;border-collapse:collapse;font-size:13px;"><thead><tr style="border-bottom:1px solid var(--border-color);text-align:left;">`;

    usageColsState.forEach((col, idx) => {
        if (!col.visible) return;
        const active = usageColFilters[col.id]?.size > 0;
        html += `<th style="padding:9px 8px;position:relative;white-space:nowrap;">${_usageEscapeHtml(col.name)} <button class="btn btn-outline" style="padding:1px 6px;font-size:10px;border-radius:7px;${active ? 'border-color:#f97316;color:#f97316;' : ''}" onclick="toggleUsageColumnFilter('${col.id}')">›</button>${renderUsageColumnMenu(col, usageDataState)} <button title="Toggle column" onclick="toggleUsageCol(${idx})" style="border:0;background:transparent;color:var(--text-muted);cursor:pointer;font-size:10px;">×</button></th>`;
    });
    html += `</tr></thead><tbody>`;
    if (filteredData.length === 0) {
        html += `<tr><td colspan="${usageColsState.filter(c => c.visible).length}" style="padding:18px;text-align:center;color:var(--text-muted);">No matching usage records for current filters.</td></tr>`;
    } else {
        const visibleCount = Math.min(usageRenderLimit, filteredData.length);
        filteredData.slice(0, visibleCount).forEach(row => {
            html += `<tr style="border-bottom:1px solid var(--border-color);">`;
            usageColsState.forEach(col => {
                if (!col.visible) return;
                const value = _usageColumnValue(col, row);
                const style = col.id === 'model' ? 'font-family:monospace;font-size:11px;' : col.id === 'provider' ? 'font-weight:700;' : col.id === 'cost' || col.id === 'savings' ? 'color:var(--brand-color);font-weight:700;' : 'color:var(--text-main);';
                html += `<td style="padding:8px;${style}">${col.id === 'provider' ? `<span class="badge">${_usageEscapeHtml(value)}</span>` : _usageEscapeHtml(value)}</td>`;
            });
            html += `</tr>`;
        });
        if (filteredData.length > visibleCount) {
            html += `<tr><td colspan="${usageColsState.filter(c => c.visible).length}" style="padding:14px;text-align:center;">
                <span style="font-size:12px;color:var(--text-muted);">Showing ${visibleCount.toLocaleString()} of ${filteredData.length.toLocaleString()} records · </span>
                <button class="btn btn-outline" style="padding:5px 12px;font-size:12px;font-weight:800;" onclick="loadMoreUsageRows()">Load 500 more</button>
            </td></tr>`;
        }
    }
    html += `</tbody></table></div></div>`;
    container.innerHTML = html;
}

async function loadLogsData() {
    try {
        const probeRes = await fetch('/api/observability/logs?limit=1&offset=0');
        const probeBody = await probeRes.json();
        const total = (probeBody && probeBody.total) ? probeBody.total : 0;
        const fetchOffset = Math.max(0, total - 2000);
        const [logsRes, artRes] = await Promise.all([
            fetch(`/api/observability/logs?limit=2000&offset=${fetchOffset}`),
            fetch('/api/observability/artifacts')
        ]);
        const logsBody = await logsRes.json();
        const artifacts = await artRes.json();
        const logs = Array.isArray(logsBody) ? logsBody : (logsBody && logsBody.entries ? logsBody.entries : []);
        logsDataState = logs.slice().reverse();
        logsArtifactsState = artifacts.slice().reverse();
        logsRenderLimit = 500;
        _logsSignature = _computeLogsSignature();
        renderLogsView();

    } catch (e) {
        const container = document.getElementById('logs-container');
        if (container) container.innerHTML = `<div class="empty-state" style="color:var(--danger-color)">Error loading logs: ${e.message}</div>`;
    }
}

// "Load 500 more" handler for the console-log view — grows logsRenderLimit.
function loadMoreLogsRows() {
    logsRenderLimit += 500;
    renderLogsView();
}

// ── Live log streaming ─────────────────────────────────────────────────
// The backend /api/observability/logs endpoint returns a JSON snapshot, so we
// poll it while the Logs tab is open to approximate 9Router's live console.
// Refreshes are skipped when the user is editing an input inside the logs view
// (to avoid clobbering in-progress edits) and when nothing has changed.
let logsLiveIntervalId = null;
let _logsSignature = '';

function _computeLogsSignature() {
    const n = logsDataState.length;
    const newest = n > 0 ? (logsDataState[0].timestamp || '') : '';
    return `${n}:${newest}`;
}

async function refreshLogsLive() {
    if (!document.getElementById('logs-container')) { stopLogsLivePolling(); return; }
    const ae = document.activeElement;
    if (ae && ['INPUT', 'SELECT', 'TEXTAREA'].includes(ae.tagName) && ae.closest && ae.closest('#logs-container')) {
        return;  // defer — user is editing a control inside the logs view
    }
    try {
        // Fetch newest 2000 logs by offsetting from the end.
        // API returns oldest-first, so we probe total then calculate offset.
        const probeRes = await fetch('/api/observability/logs?limit=1&offset=0');
        const probeBody = await probeRes.json();
        const total = (probeBody && probeBody.total) ? probeBody.total : 0;
        const fetchOffset = Math.max(0, total - 2000);
        const [logsRes, artRes] = await Promise.all([
            fetch(`/api/observability/logs?limit=2000&offset=${fetchOffset}`),
            fetch('/api/observability/artifacts')
        ]);
        if (!logsRes.ok) return;
        const logsBody = await logsRes.json();
        const artifacts = await artRes.json();
        const logs = Array.isArray(logsBody) ? logsBody : (logsBody && logsBody.entries ? logsBody.entries : []);
        // Reverse oldest-first → newest-first for display.
        logsDataState = logs.slice().reverse();
        logsArtifactsState = artifacts.slice().reverse();
        const sig = _computeLogsSignature();
        if (sig !== _logsSignature) {
            _logsSignature = sig;
            // Keep the user's current logsRenderLimit (expanded view survives),
            // but re-apply the cap to the newest rows by re-rendering.
            renderLogsView();
        }
    } catch (e) { /* transient — next tick retries */ }
}

function startLogsLivePolling() {
    stopLogsLivePolling();
    logsLiveIntervalId = setInterval(refreshLogsLive, 2000);
}

function stopLogsLivePolling() {
    if (logsLiveIntervalId) {
        clearInterval(logsLiveIntervalId);
        logsLiveIntervalId = null;
    }
}

let autoClearLogsIntervalId = null;

function setupAutoClearInterval() {
    if (autoClearLogsIntervalId) {
        clearInterval(autoClearLogsIntervalId);
        autoClearLogsIntervalId = null;
    }
    const intervalMinutes = globalConfig.tools?.auto_clear_logs_interval || 0;
    if (intervalMinutes > 0) {
        autoClearLogsIntervalId = setInterval(async () => {
            try {
                const res = await fetch('/api/observability/clear_logs', { method: 'POST' });
                if (res.ok) {
                    logsDataState = [];
                    logsArtifactsState = [];
                    logsRenderLimit = 500;
                    if (document.getElementById('logs-container')) renderLogsView();
                }
            } catch (e) {
                console.error("Auto clear failed:", e);
            }
        }, intervalMinutes * 60 * 1000);
    }
}

window.updateAutoClearInterval = async function(minutes) {
    if (!globalConfig.tools) globalConfig.tools = {};
    globalConfig.tools.auto_clear_logs_interval = parseInt(minutes, 10);
    await saveGlobalConfig();
    setupAutoClearInterval();
};

// ── Console-log route-text grammar (single source of truth) ────────────────
// Strips the canonical route prefix so rows show only the resolved chain.
// Includes legacy BSL forms so persisted pre-rename entries still render cleanly.
function stripRoutePrefix(t) {
    return t.replace(/^(Combo > |Blacksand-Chat > |Blacksand-Lite > |Blacksand-Agentic > |Blacksand-Agentic-Ultra > |Blacksand-Agentic-Max > |BSL Chat > |BSL Lite > |BSL-Chat > |BSL-Lite > |BSL-Agentic > |BSL-Agentic-Ultra > |BSL-Agentic-Max > )/, '');
}
function _isBslRouteEvent(ev) {
    return ev === 'bsl_chat_route' || ev === 'bsl_lite_route' ||
        ev === 'bsl_agentic_route' || ev === 'bsl_agentic_ultra_route' || ev === 'bsl_agentic_max_route';
}
function _bslRouteLabel(ev) {
    if (ev === 'bsl_chat_route') return 'Blacksand-Chat';
    if (ev === 'bsl_lite_route') return 'Blacksand-Lite';
    if (ev === 'bsl_agentic_route') return 'Blacksand-Agentic';
    if (ev === 'bsl_agentic_ultra_route') return 'Blacksand-Agentic-Ultra';
    if (ev === 'bsl_agentic_max_route') return 'Blacksand-Agentic-Max';
    return 'COMBO';
}

// ── Lifecycle merge: link cache_tracker + start events to their end events ─────
function _buildLifecycleRows(rawData) {
    const cacheTrackers = rawData.filter(r => r.event === 'cache_tracker');
    const endEvents     = rawData.filter(r => r.event === 'end');
    const startEvents   = rawData.filter(r => r.event === 'start');
    const endRequestIds = new Set(endEvents.map(r => r.request_id).filter(Boolean));

    // Build a map: `provider/model/tsMs` → strategy+hint for fast lookup
    const trackerMap = new Map();
    cacheTrackers.forEach(ct => {
        const key = `${ct.provider}/${ct.model}`;
        if (!trackerMap.has(key)) trackerMap.set(key, []);
        trackerMap.get(key).push(ct);
    });

    // Enrich end events with cache info
    const enriched = endEvents.map(ev => {
        const key = `${ev.provider}/${ev.model}`;
        const candidates = trackerMap.get(key) || [];
        const evMs = new Date(ev.timestamp).getTime();
        const match = candidates.find(ct => {
            const ctMs = new Date(ct.timestamp).getTime();
            return Math.abs(evMs - ctMs) < 3000;
        });
        return match ? { ...ev, _cache_strategy: match.strategy, _cache_hint: match.cache_hint } : { ...ev };
    });

    // Route decisions emitted immediately before their matching start event.
    const routeDecisions = rawData.filter(r =>
        (r.event === 'bsl_chat_route' || r.event === 'bsl_lite_route' ||
         r.event === 'bsl_agentic_route' || r.event === 'bsl_agentic_ultra_route' ||
         r.event === 'bsl_agentic_max_route' || r.event === 'combo_route') &&
        r.timestamp && r.text
    );

    const endByRequestId = {};
    for (const e of enriched) {
        if (e.request_id) endByRequestId[e.request_id] = e;
    }

    for (const route of routeDecisions) {
        const routeMs = new Date(route.timestamp).getTime();
        const matchingStart = startEvents.find(start => {
            if (route.request_id && start.request_id === route.request_id) return true;
            return Math.abs(new Date(start.timestamp).getTime() - routeMs) < 100;
        });
        if (!matchingStart) continue;

        matchingStart._route_text = stripRoutePrefix(route.text);
        matchingStart._route_event = route.event;

        if (matchingStart.request_id && endByRequestId[matchingStart.request_id]) {
            const matchingEnd = endByRequestId[matchingStart.request_id];
            matchingEnd._route_text = matchingStart._route_text;
            matchingEnd._route_event = route.event;
            if (route.event === 'bsl_chat_route' || route.event === 'bsl_lite_route' ||
                route.event === 'bsl_agentic_route' || route.event === 'bsl_agentic_ultra_route' ||
                route.event === 'bsl_agentic_max_route') {
                matchingEnd._bsl_route = matchingStart._route_text;
            }
        }

        route._consumed = true;
    }

    const remainingRoutes = routeDecisions.filter(r => !r._consumed);
    const pendingStarts = startEvents
        .filter(s => s.request_id && !endRequestIds.has(s.request_id))
        .map(s => ({ ...s, _pending: true }));

    const all = [...pendingStarts, ...enriched, ...remainingRoutes];
    all.sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());
    return all;
}

function renderLogsView() {
    const container = document.getElementById('logs-container');
    if (!container) return;

    const filterStr = logsFilterState.toLowerCase();
    const lifecycleRows = _buildLifecycleRows(logsDataState);
    const filteredData = lifecycleRows.filter(row => {
        if (!filterStr) return true;
        const rowStr = Object.values(row).join(' ').toLowerCase();
        return rowStr.includes(filterStr);
    });
    
    const errorLogs = filteredData.filter(log => !!log.error || log.status >= 400);
    const formatLogLine = (log) => {
        const d = new Date(log.timestamp);
        const timeStr = `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}:${d.getSeconds().toString().padStart(2, '0')}.${d.getMilliseconds().toString().padStart(3, '0')}`;

        // ── ROUTE row (routing decision) — only unconsumed standalone routes ──
        if (_isBslRouteEvent(log.event) || log.event === 'combo_route') {
            const label = _isBslRouteEvent(log.event) ? _bslRouteLabel(log.event) : 'COMBO';
            const rawText = stripRoutePrefix(log.text || '');
            const text = _usageEscapeHtml(rawText);
            const color = _isBslRouteEvent(log.event) ? '#38bdf8' : '#0ea5e9';
            return `<div style="opacity:0.85;"><span style="color:var(--text-muted)">[${timeStr}]</span> <span style="color:${color};">🔀 ${label}</span> ${text}</div>`;
        }

        // ── PENDING row (in-flight start event, no end yet) ───────────────
        if (log._pending) {
            const isStream = log.stream === true;
            const streamTag = isStream ? '<span style="color:#0e7490;">stream:true</span>' : '<span style="color:var(--text-muted);">stream:false</span>';
            let routeLabel;
            if (log._route_text) {
                const isPendingBsl = _isBslRouteEvent(log._route_event);
                const pendingRouteColor = isPendingBsl ? '#38bdf8' : '#22c55e';
                routeLabel = `<span style="color:${pendingRouteColor};">🔀 ${_usageEscapeHtml(log._route_text)}</span>`;
            } else {
                const provModel = _usageEscapeHtml(`${log.provider || '?'}/${log.model || '?'}`);
                const _pendingComboKey = log.combo ? _usageEscapeHtml(log.combo) : null;
                const _pendingProvModel = `${log.provider || '?'}/${log.model || '?'}`;
                routeLabel = (_pendingComboKey && _pendingComboKey !== _pendingProvModel)
                    ? `<span style="color:var(--brand-color)">${_pendingComboKey}</span> <span style="color:var(--text-muted);font-size:11px;">›</span> <span style="color:var(--text-muted);font-size:11px;">${provModel}</span>`
                    : `<span style="color:var(--brand-color)">${provModel}</span>`;
            }
            return `<div style="opacity:0.65;"><span style="color:var(--text-muted)">[${timeStr}]</span> <span style="color:#f59e0b;">⏳ PENDING</span> ${streamTag} ${routeLabel} | waiting…</div>`;
        }

        // ── END row (normal completed request) ────────────────────────────
        const status = log.status || 0;
        const isBslModels = _isBslRouteEvent(log._route_event);
        const statusColor = status >= 400 ? '#f87171' : (isBslModels ? '#38bdf8' : '#22c55e');
        const ttft = typeof log.ttft_ms === 'number' ? log.ttft_ms.toFixed(0) : '—';
        const total = typeof log.total_time_ms === 'number' ? log.total_time_ms.toFixed(0) : '—';
        const inTok = log.in_tokens ?? '—';
        const outTok = log.out_tokens ?? '—';
        const cached = log.cached_tokens ?? 0;
        const isStream = log.stream === true;
        const errStr = log.error ? ` <span style="color:#f87171;">| ERROR:</span> <span style="color:var(--text-muted);">${_usageEscapeHtml(log.error)}</span>` : '';
        // Thinking config (effort, reasoning_mode, reasoning_context) — compact inline badge
        let thinkStr = '';
        if (log.thinking && typeof log.thinking === 'object') {
            const parts = [];
            if (log.thinking.effort && log.thinking.effort !== 'auto') parts.push(log.thinking.effort);
            if (log.thinking.reasoning_mode) parts.push('mode:' + log.thinking.reasoning_mode);
            if (log.thinking.reasoning_context) parts.push('ctx:' + log.thinking.reasoning_context);
            if (parts.length) thinkStr = ` <span style="color:#0d9488;" title="Thinking config applied">| 🧠 ${_usageEscapeHtml(parts.join(', '))}</span>`;
        }
        // Cache strategy badge (from merged cache_tracker event)
        let cacheStr = '';
        if (log._cache_strategy && log._cache_strategy !== 'none' && log._cache_strategy !== 'implicit') {
            const hint = log._cache_hint ? ` (${log._cache_hint})` : '';
            cacheStr = ` <span style="color:#0891b2;font-size:11px;" title="Cache strategy: ${_usageEscapeHtml(log._cache_strategy)}${hint}">| 💾 ${_usageEscapeHtml(log._cache_strategy)}${_usageEscapeHtml(hint)}</span>`;
        }
        // Combo ancestry — resolution chain: combo › provider/model
        const provModel = _usageEscapeHtml(`${log.provider}/${log.model}`);
        let routeLabel;
        const isError = status >= 400;
        const errorRouteColor = '#f87171'; // light red for error model badge
        if (log._route_text) {
            const routeText = _usageEscapeHtml(log._route_text);
            const isBslRoute = _isBslRouteEvent(log._route_event);
            // No prefix label needed - stripRoutePrefix already removed the
            // Blacksand-X > prefix, and the route text starts with the selected model.
            const routeColor = isError ? errorRouteColor : (isBslRoute ? '#38bdf8' : '#22c55e');
            routeLabel = `<span style="color:${routeColor};">🔀 ${routeText}</span>`;
        } else {
            const _endComboKey = log.combo ? _usageEscapeHtml(log.combo) : null;
            const _endProvModel = `${log.provider || '?'}/${log.model || '?'}`;
            const comboColor = isError ? errorRouteColor : '#22c55e';
            routeLabel = (_endComboKey && _endComboKey !== _endProvModel)
                ? `<span style="color:${comboColor}">${_endComboKey}</span> <span style="color:var(--text-muted);font-size:11px;">›</span> <span style="color:var(--text-muted);font-size:11px;">${provModel}</span>`
                : `<span style="color:${comboColor}">${provModel}</span>`;
        }
        const streamTag = isStream ? '<span style="color:#0e7490;">stream:true</span>' : '<span style="color:var(--text-muted);">stream:false</span>';
        return `<div><span style="color:var(--text-muted)">[${timeStr}]</span> <span style="color:${statusColor};">[${status}]</span> ${streamTag} ${routeLabel} | ${total}ms${ttft !== '—' ? ` (TTFT ${ttft})` : ''} | In: ${inTok}${cached > 0 ? ` <span style="color:var(--success-color)">(${cached} cached)</span>` : ''} | Out: ${outTok}${thinkStr}${cacheStr}${errStr}</div>`;
    };
    
    // _buildLifecycleRows already sorts oldest-first, so the newest entries
    // sit at the BOTTOM — matching the autoscroll-to-bottom behavior below.
    const chronological = [...filteredData];
    const chronologicalErrors = [...errorLogs];
    // Cap rendered console rows so a large log set doesn't freeze the tab.
    // The newest entries sit at the bottom, so when capping we keep the tail
    // (most recent) and offer a "Load 500 more" button to grow the window.
    const _visibleLogCount = Math.min(logsRenderLimit, chronological.length);
    const visibleChronological = chronological.slice(Math.max(0, chronological.length - _visibleLogCount));
    const consoleLogLines = visibleChronological.map(formatLogLine).join('')
        + (chronological.length > _visibleLogCount
            ? `<div id="logs-load-more-row" style="padding:8px;text-align:center;border-top:1px solid #333;color:#9ca3af;font-size:11px;">
                 Showing ${_visibleLogCount.toLocaleString()} of ${chronological.length.toLocaleString()} entries ·
                 <button class="btn btn-outline" style="padding:4px 10px;font-size:11px;font-weight:800;" onclick="loadMoreLogsRows()">Load 500 more</button>
               </div>`
            : '');
    const errorLogLines = chronologicalErrors.map(formatLogLine).join('');
    
    const autoClearInterval = globalConfig.tools?.auto_clear_logs_interval || 0;
    const prolongBtn = (id, defaultH) => `<button onclick="prolongLogBox('${id}','${defaultH}')" style="background:none;border:none;cursor:pointer;padding:2px 6px;color:var(--text-muted);" title="Expand height by 100px">
        <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="7 13 12 18 17 13"/><polyline points="7 6 12 1 17 6"/><line x1="12" y1="1" x2="12" y2="18"/></svg>
    </button>`;
    const collapseIcon = (id) => `<button onclick="toggleLogBox('${id}')" style="background:none;border:none;cursor:pointer;padding:2px 6px;color:var(--text-muted);" title="Collapse/Expand">
        <svg id="chevron-${id}" viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="18 15 12 9 6 15"/></svg>
    </button>`;

    // Auto Error Prevention config — Logs tab owns the operational controls so
    // error triage stays in one vertical flow: Console → Error Log → Fix Settings → Report.
    if (!globalConfig.error_prevention) globalConfig.error_prevention = {};
    const ep = globalConfig.error_prevention;
    if (ep.enabled === undefined) ep.enabled = false;
    if (ep.consecutive_threshold === undefined) ep.consecutive_threshold = 3;
    if (ep.softban_duration_minutes === undefined) ep.softban_duration_minutes = 5;
    if (ep.longban_duration_minutes === undefined) ep.longban_duration_minutes = 60;
    if (ep.rate_limit_cooldown_seconds === undefined) ep.rate_limit_cooldown_seconds = 90;
    if (ep.disable_after_longban === undefined) ep.disable_after_longban = true;
    if (ep.notification_enabled === undefined) ep.notification_enabled = true;
    const errorFixSettingsHtml = `
        <div class="settings-section" style="margin-bottom:0;border-color:${ep.enabled ? 'rgba(34,197,94,0.30)' : 'var(--border-color)'};">
            <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px;">
                <div>
                    <h2 class="section-title" style="margin:0;">⚡ Error Fix Settings</h2>
                    <div class="setting-desc" style="margin-top:4px;">Auto Error Prevention controls for soft-ban, long-ban, disable, and notifications.</div>
                </div>
                <div style="display:flex;align-items:center;gap:10px;">
                    <span style="font-size:11px;font-weight:800;color:${ep.enabled ? 'var(--success-color,#16a34a)' : 'var(--text-muted)'};text-transform:uppercase;">${ep.enabled ? 'Active' : 'Paused'}</span>
                    <label class="switch">
                        <input type="checkbox" onchange="globalConfig.error_prevention.enabled = this.checked; renderLogsView(); saveGlobalConfig();" ${ep.enabled ? 'checked' : ''}>
                        <span class="slider"></span>
                    </label>
                </div>
            </div>
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;">
                <label style="display:flex;flex-direction:column;gap:6px;font-size:12px;color:var(--text-muted);font-weight:700;">Consecutive errors
                    <input type="number" min="1" max="20" class="input" value="${ep.consecutive_threshold}" onchange="globalConfig.error_prevention.consecutive_threshold = Math.max(1, parseInt(this.value)||3); saveGlobalConfig();">
                </label>
                <label style="display:flex;flex-direction:column;gap:6px;font-size:12px;color:var(--text-muted);font-weight:700;">Soft-ban minutes
                    <input type="number" min="1" max="120" class="input" value="${ep.softban_duration_minutes}" onchange="globalConfig.error_prevention.softban_duration_minutes = Math.max(1, parseInt(this.value)||5); saveGlobalConfig();">
                </label>
                <label style="display:flex;flex-direction:column;gap:6px;font-size:12px;color:var(--text-muted);font-weight:700;">Long-ban minutes
                    <input type="number" min="5" max="1440" class="input" value="${ep.longban_duration_minutes}" onchange="globalConfig.error_prevention.longban_duration_minutes = Math.max(5, parseInt(this.value)||60); saveGlobalConfig();">
                </label>
                <label style="display:flex;flex-direction:column;gap:6px;font-size:12px;color:var(--text-muted);font-weight:700;">429 cooldown seconds
                    <input type="number" min="15" max="600" class="input" value="${ep.rate_limit_cooldown_seconds}" onchange="globalConfig.error_prevention.rate_limit_cooldown_seconds = Math.max(15, parseInt(this.value)||90); saveGlobalConfig();">
                </label>
                <div style="display:flex;flex-direction:column;gap:10px;justify-content:center;">
                    <label style="display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:12px;font-weight:700;color:var(--text-main);">Disable after long-ban
                        <input type="checkbox" onchange="globalConfig.error_prevention.disable_after_longban = this.checked; saveGlobalConfig();" ${ep.disable_after_longban !== false ? 'checked' : ''}>
                    </label>
                    <label style="display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:12px;font-weight:700;color:var(--text-main);">Desktop notifications
                        <input type="checkbox" onchange="globalConfig.error_prevention.notification_enabled = this.checked; if(this.checked) requestPushPermission(); saveGlobalConfig();" ${ep.notification_enabled ? 'checked' : ''}>
                    </label>
                </div>
            </div>
            <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;margin-top:12px;padding-top:12px;border-top:1px solid var(--border-color);flex-wrap:wrap;">
                <div style="font-size:12px;color:var(--text-muted);">Flow: 429/rate-limit → immediate short cooldown; repeated non-429 errors → soft-ban → long-ban → ${ep.disable_after_longban !== false ? 'disable model' : 'warning only'}.<br>Ban controls are in the <strong>Error Report</strong> section below.</div>
                <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                    <button class="btn btn-primary" style="padding:5px 12px;font-size:12px;" onclick="saveGlobalConfig()">Save Error Fix Settings</button>
                </div>
            </div>
        </div>`;
    
    // Build Error Report table
    const classifyError = (errMsg) => {
        if (!errMsg) return 'Unknown';
        const e = errMsg.toLowerCase();
        if (e.includes('429') || e.includes('rate limit') || e.includes('quota') || e.includes('exceeded') || e.includes('ascii codec') || e.includes('1302') || e.includes('速率限制') || e.includes('您的账户已达到') || e.includes('请求频率')) return 'Rate Limited';
        if (e.includes('timeout') || e.includes('timed out')) return 'Timeout';
        if (e.includes('401') || e.includes('unauthorized') || e.includes('forbidden') || e.includes('403')) return 'Auth Error';
        if (e.includes('404') || e.includes('not found')) return 'Not Found';
        if (e.includes('500') || e.includes('server error')) return 'Server Error';
        if (e.includes('connect') || e.includes('refused')) return 'Connection Refused';
        if (e.includes('model') || e.includes('invalid')) return 'Model Error';
        return 'Unknown';
    };
    const allErrors = logsDataState.filter(log => !!log.error || (log.status || 0) >= 400);
    const errorTypeCounts = {};
    allErrors.forEach(log => {
        const t = classifyError(log.error);
        errorTypeCounts[t] = (errorTypeCounts[t] || 0) + 1;
    });
    const topErrorType = Object.entries(errorTypeCounts).sort((a,b) => b[1]-a[1])[0]?.[0] || '—';
    const affectedModels = [...new Set(allErrors.map(l => l.model))].length;
    
    const reportRows = allErrors.map(log => {
        const d = new Date(log.timestamp);
        const dateStr = d.toLocaleDateString();
        const timeStr = `${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}:${d.getSeconds().toString().padStart(2,'0')}`;
        const errType = classifyError(log.error);
        const canSelfHeal = ['Auth Error','Rate Limited','Timeout','Model Error'].includes(errType);
        const banCell = renderBanStatusCell(log.model, log.provider);
        return `
            <tr style="border-bottom:1px solid var(--border-color);">
                <td style="padding:7px 8px;">
                    <span style="background:${errType==='Auth Error'?'#fee2e2':errType==='Rate Limited'?'#fef9c3':errType==='Timeout'?'#ffe4e6':'#f3f4f6'};color:${errType==='Auth Error'?'#dc2626':errType==='Rate Limited'?'#ca8a04':errType==='Timeout'?'#e11d48':'var(--text-muted)'};padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;">${errType}</span>
                </td>
                <td style="padding:7px 8px;font-family:monospace;font-size:12px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${_usageEscapeHtml(log.model)}">${_usageEscapeHtml(log.model)}</td>
                <td style="padding:7px 8px;font-size:12px;">${_usageEscapeHtml(log.provider)}</td>
                <td style="padding:7px 8px;font-size:12px;color:var(--text-muted);">${dateStr}</td>
                <td style="padding:7px 8px;font-size:12px;color:var(--text-muted);">${timeStr}</td>
                <td style="padding:7px 8px;font-size:12px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--danger-color);" title="${_usageEscapeHtml(log.error || '')}">${_usageEscapeHtml(log.error || `HTTP ${log.status}`)}</td>
                <td style="padding:7px 8px;">${banCell}</td>
                <td style="padding:7px 8px;">${canSelfHeal ? `<span style="font-size:11px;color:var(--brand-color);cursor:pointer;" onclick="selfHealError('${_usageEscapeHtml(log.model)}','${_usageEscapeHtml(log.provider)}','${errType}')">&#9889; Auto-fix</span>` : '<span style="font-size:11px;color:var(--text-muted);">Manual</span>'}</td>
            </tr>`;
    }).join('');
    
    let html = `
        <div style="display:flex;flex-direction:column;gap:16px;margin-bottom:24px;">
            <div class="settings-section" style="margin-bottom:0;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; gap:12px; flex-wrap:wrap;">
                    <h2 class="section-title" style="margin:0;">Console Log <span style="font-size:12px;color:var(--text-muted);font-weight:400;">(${filteredData.length} entries)</span></h2>
                    <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap; justify-content:flex-end;">
                        <div style="display:flex; align-items:center; gap:6px;">
                            <span style="font-size:12px; color:var(--text-muted)">Auto-clear:</span>
                            <select class="input" style="padding:4px 8px; font-size:12px; width:auto;" onchange="updateAutoClearInterval(this.value)">
                                <option value="0" ${autoClearInterval === 0 ? 'selected' : ''}>Off</option>
                                <option value="5" ${autoClearInterval === 5 ? 'selected' : ''}>5 min</option>
                                <option value="15" ${autoClearInterval === 15 ? 'selected' : ''}>15 min</option>
                                <option value="30" ${autoClearInterval === 30 ? 'selected' : ''}>30 min</option>
                                <option value="60" ${autoClearInterval === 60 ? 'selected' : ''}>1 hour</option>
                            </select>
                        </div>
                        <button class="btn btn-outline" onclick="loadLogsData()" style="padding:4px 8px; font-size:12px;">Refresh</button>
                        <button class="btn btn-outline" onclick="clearLogs()" style="padding:4px 8px; font-size:12px; color:var(--danger-color); border-color:var(--danger-color)">Clear</button>
                        ${prolongBtn('console-log-box','240px')}
                        ${collapseIcon('console-log-box')}
                    </div>
                </div>
                
                <div style="margin-bottom:12px;">
                    <input type="text" id="logs-search-input" class="input" placeholder="Search logs..." style="width:100%; font-size:12px;" value="${_usageEscapeHtml(logsFilterState)}" oninput="_logsFilterUpdate(this.value)">
                </div>
                
                <div id="console-log-box" style="background: #1e1e1e; color: #d4d4d4; font-family: monospace; font-size: 12px; padding: 12px; border-radius: 8px; height: 240px; overflow-y: auto; border: 1px solid var(--border-color); white-space: pre-wrap; word-break: break-word; transition: height 0.3s;">
                    ${consoleLogLines || '<div style="color:var(--text-muted)">No logs available.</div>'}
                </div>
            </div>

            <div class="settings-section" style="margin-bottom:0;border-color:${errorLogs.length ? 'rgba(239,68,68,0.35)' : 'var(--border-color)'};box-shadow:${errorLogs.length ? '0 10px 28px rgba(239,68,68,0.08)' : '0 1px 3px rgba(0,0,0,0.05)'};">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;">
                    <h2 class="section-title" style="margin:0;">Error Log ${errorLogs.length > 0 ? `<span style="background:var(--danger-color,#ef4444);color:#fff;font-size:11px;padding:2px 7px;border-radius:10px;font-weight:600;margin-left:8px;vertical-align:middle;">${errorLogs.length}</span>` : ''}</h2>
                    <div style="display:flex; align-items:center; gap:4px;">
                        ${prolongBtn('error-log-box','240px')}
                        ${collapseIcon('error-log-box')}
                    </div>
                </div>
                <div id="error-log-box" style="background: #1e1e1e; color: #d4d4d4; font-family: monospace; font-size: 12px; padding: 12px; border-radius: 8px; height: 240px; overflow-y: auto; border: 1px solid var(--border-color); white-space: pre-wrap; word-break: break-word; transition: height 0.3s;">
                    ${errorLogLines || '<div style="color:var(--text-muted)">No errors recorded.</div>'}
                </div>
            </div>

            ${errorFixSettingsHtml}
        </div>
        
        <div class="settings-section" style="margin-top: 0;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;">
                <h2 class="section-title" style="margin:0;">Error Report ${allErrors.length > 0 ? `<span style="background:var(--danger-color,#ef4444);color:#fff;font-size:11px;padding:2px 7px;border-radius:10px;font-weight:600;margin-left:8px;vertical-align:middle;">${allErrors.length}</span>` : ''}</h2>
                <div style="display:flex; gap:8px; align-items:center;">
                    <button class="btn btn-outline" style="padding:4px 10px; font-size:12px;" onclick="exportErrorReport()">&#8595; Export CSV</button>
                    <button class="btn btn-outline" style="padding:4px 10px; font-size:12px; display:flex; align-items:center; gap:5px;" onclick="clearAllBans()" title="Resets active soft/long bans only. Does NOT re-enable disabled models.">&#9851; Reset Temp Bans</button>
                    <button class="btn btn-outline" style="padding:4px 10px; font-size:12px; display:flex; align-items:center; gap:5px; color:var(--danger-color,#ef4444); border-color:var(--danger-color,#ef4444);" onclick="liftAllBans()" title="Clears ALL temp bans AND re-enables models disabled by Error Prevention. Manually disabled models stay disabled.">&#9889; Lift All Bans</button>
                    <button class="btn btn-primary" onclick="triggerErrorAnalysis()" style="padding:4px 10px; font-size:12px;">&#127916; AI Report</button>
                    ${prolongBtn('error-report-box','auto')}
                    ${collapseIcon('error-report-box')}
                </div>
            </div>
            <div id="error-report-box" style="transition: height 0.3s; overflow:hidden;">
                ${allErrors.length > 0 ? `
                <div style="display:flex; gap:16px; margin-bottom:12px; flex-wrap:wrap;">
                    <div style="background:var(--bg-body);border:1px solid var(--border-color);border-radius:8px;padding:10px 16px;flex:1;min-width:120px;">
                        <div style="font-size:11px;color:var(--text-muted);">Total Errors</div>
                        <div style="font-size:20px;font-weight:700;color:var(--danger-color,#ef4444);">${allErrors.length}</div>
                    </div>
                    <div style="background:var(--bg-body);border:1px solid var(--border-color);border-radius:8px;padding:10px 16px;flex:1;min-width:120px;">
                        <div style="font-size:11px;color:var(--text-muted);">Top Error Type</div>
                        <div style="font-size:16px;font-weight:700;color:var(--text-main);">${topErrorType}</div>
                    </div>
                    <div style="background:var(--bg-body);border:1px solid var(--border-color);border-radius:8px;padding:10px 16px;flex:1;min-width:120px;">
                        <div style="font-size:11px;color:var(--text-muted);">Affected Models</div>
                        <div style="font-size:20px;font-weight:700;color:var(--text-main);">${affectedModels}</div>
                    </div>
                </div>
                <div style="overflow-x:auto;">
                <table style="width:100%;border-collapse:collapse;font-size:13px;">
                    <thead><tr style="border-bottom:2px solid var(--border-color);text-align:left;">
                        <th style="padding:8px;">Error Type</th>
                        <th style="padding:8px;">Model ID</th>
                        <th style="padding:8px;">Provider</th>
                        <th style="padding:8px;">Date</th>
                        <th style="padding:8px;">Time</th>
                        <th style="padding:8px;">Details</th>
                        <th style="padding:8px;">Ban Status</th>
                        <th style="padding:8px;">Action</th>
                    </tr></thead>
                    <tbody>${reportRows}</tbody>
                </table>
                </div>` : '<div style="font-size:13px;color:var(--text-muted);padding:16px 0;">No errors in current log.</div>'}
            </div>
        </div>
    `;
    
    // Artifacts folded INTO the Error Report section — no separate section header
    html += `
        <div class="settings-section" style="margin-top: 24px;">
            <h2 class="section-title" style="margin:0; margin-bottom:12px; font-size:13px; color:var(--text-muted); font-weight:600;">AI Analysis Artifacts</h2>
            <div style="display:flex; flex-direction:column; gap:8px;">
    `;
    if (logsArtifactsState.length === 0) {
        html += `<div class="empty-state">No AI analysis reports yet. Use the &#127916; AI Report button in Error Report to generate one.</div>`;
    } else {
        logsArtifactsState.forEach(art => {
            const date = new Date(art.timestamp).toLocaleString();
            html += `
                <div style="padding:12px; border:1px solid var(--border-color); border-radius:6px; display:flex; justify-content:space-between; align-items:center; background:var(--surface-color)">
                    <div>
                        <div style="font-weight:600; font-size:14px; margin-bottom:4px;">${art.filename}</div>
                        <div style="font-size:12px; color:var(--text-muted)">${date} • Analyzed ${art.error_count} errors</div>
                    </div>
                    <a href="/api/observability/artifact/${art.filename}" target="_blank" class="btn btn-outline" style="padding:4px 12px; font-size:12px; text-decoration:none;">View Report</a>
                </div>
            `;
        });
    }
    html += `</div></div>`;
    
    // ── Smart DOM update: preserve search input focus ─────────────────────────
    // Full innerHTML rebuild on every poll cycle destroys activeElement focus.
    // Strategy: diff the scaffold signature. If the scaffold hasn't changed
    // (user hasn't navigated away), only swap the log content divs in-place.
    // NOTE: Pending-start count is intentionally excluded from the scaffold sig.
    // It changes every second (starts arrive without ends during health probes)
    // and would force a full DOM rebuild + scroll reset on every poll tick.
    // Only error count (structural) triggers a full rebuild.
    const scaffoldSig = `errors:${errorLogs.length}`;
    const prevSig = container.dataset.logScaffoldSig;
    const ae = document.activeElement;
    const searchHasFocus = ae && ae.id === 'logs-search-input';
    const cursorPos = searchHasFocus ? ae.selectionStart : -1;

    const captureLogScrollState = (box) => ({
        existed: Boolean(box),
        atBottom: Boolean(box) && box.scrollHeight - box.scrollTop - box.clientHeight < 60,
        scrollTop: box ? box.scrollTop : 0,
    });
    const restoreLogScroll = (box, beforePatch) => {
        if (!box) return;
        if (!beforePatch.existed || beforePatch.atBottom) {
            box.scrollTop = box.scrollHeight;
            return;
        }
        box.scrollTop = Math.min(beforePatch.scrollTop, Math.max(0, box.scrollHeight - box.clientHeight));
    };
    const consoleScrollBeforePatch = captureLogScrollState(container.querySelector('#console-log-box'));
    const errorScrollBeforePatch = captureLogScrollState(container.querySelector('#error-log-box'));

    if (prevSig === scaffoldSig && container.querySelector('#console-log-box')) {
        // Scaffold is stable - patch only the content boxes to preserve focus.
        const existingConsole = container.querySelector('#console-log-box');
        const existingError   = container.querySelector('#error-log-box');
        if (existingConsole) existingConsole.innerHTML = consoleLogLines || '<div style="color:var(--text-muted)">No logs available.</div>';
        if (existingError)   existingError.innerHTML   = errorLogLines   || '<div style="color:var(--text-muted)">No errors recorded.</div>';
        // Restore search input value without moving focus.
        const inp = document.getElementById('logs-search-input');
        if (inp && inp.value !== logsFilterState) inp.value = logsFilterState;
    } else {
        container.innerHTML = html;
        container.dataset.logScaffoldSig = scaffoldSig;
        // Restore search focus + cursor if it was active before the rebuild.
        if (searchHasFocus) {
            const inp = document.getElementById('logs-search-input');
            if (inp) { inp.focus(); if (cursorPos >= 0) { try { inp.setSelectionRange(cursorPos, cursorPos); } catch(_) {} } }
        }
    }

    restoreLogScroll(container.querySelector('#console-log-box'), consoleScrollBeforePatch);
    restoreLogScroll(container.querySelector('#error-log-box'), errorScrollBeforePatch);
}

// Debounced search update — updates state and re-renders log content only,
// never full scaffold, so the input element retains focus across keystrokes.
window._logsFilterUpdate = function(val) {
    logsFilterState = val;
    logsRenderLimit = 500;
    // Only patch the content boxes (fast path) — no full rebuild needed
    const consoleBox = document.getElementById('console-log-box');
    const errorBox   = document.getElementById('error-log-box');
    if (!consoleBox) { renderLogsView(); return; }
    const filterStr = val.toLowerCase();
    const lifecycleRows = _buildLifecycleRows(logsDataState);
    const filteredData  = lifecycleRows.filter(row => {
        if (!filterStr) return true;
        return Object.values(row).join(' ').toLowerCase().includes(filterStr);
    });
    const errorLogs = filteredData.filter(log => !!log.error || (log.status || 0) >= 400);
    // Re-use formatLogLine by calling renderLogsView which will take the smart patch path
    renderLogsView();
};

window.toggleLogBox = function(id) {
    const box = document.getElementById(id);
    const chevron = document.getElementById('chevron-' + id);
    if (!box) return;
    const isCollapsed = box.style.height === '0px' || box.dataset.collapsed === '1';
    if (isCollapsed) {
        box.style.height = box.dataset.expandedHeight || '300px';
        box.dataset.collapsed = '0';
        if (chevron) chevron.setAttribute('points', '18 15 12 9 6 15');
    } else {
        if (!box.dataset.expandedHeight) box.dataset.expandedHeight = box.style.height || getComputedStyle(box).height;
        box.style.height = '0px';
        box.dataset.collapsed = '1';
        if (chevron) chevron.setAttribute('points', '6 9 12 15 18 9');
    }
};

window.prolongLogBox = function(id, defaultH) {
    const box = document.getElementById(id);
    if (!box) return;
    // If collapsed, expand first
    if (box.dataset.collapsed === '1') { toggleLogBox(id); return; }
    // For the error-report box (height:auto), switch to a fixed scrollable height first
    let current = parseInt(box.style.height, 10);
    if (isNaN(current)) {
        current = box.offsetHeight;
        box.style.overflowY = 'auto';
    }
    const next = current + 150;
    // Cap at 900px, then cycle back to the default
    if (next > 900) {
        box.style.height = (defaultH === 'auto') ? 'auto' : defaultH;
        if (defaultH === 'auto') box.style.overflowY = 'visible';
    } else {
        box.style.height = next + 'px';
    }
    box.dataset.expandedHeight = box.style.height;
};

window.exportErrorReport = function() {
    const allErrors = logsDataState.filter(log => !!log.error || (log.status || 0) >= 400);
    if (allErrors.length === 0) { showToast('No errors to export', true); return; }
    const header = 'Error Type,Model ID,Provider,Date,Time,Details\n';
    const classifyError = (errMsg) => {
        if (!errMsg) return 'Unknown';
        const e = errMsg.toLowerCase();
        if (e.includes('timeout')) return 'Timeout';
        if (e.includes('401') || e.includes('unauthorized') || e.includes('403')) return 'Auth Error';
        if (e.includes('429') || e.includes('rate limit')) return 'Rate Limited';
        if (e.includes('404')) return 'Not Found';
        if (e.includes('500')) return 'Server Error';
        if (e.includes('connect') || e.includes('refused')) return 'Connection Refused';
        return 'Unknown';
    };
    const rows = allErrors.map(log => {
        const d = new Date(log.timestamp);
        const details = (log.error || `HTTP ${log.status}`).replace(/,/g, ';').replace(/\n/g, ' ');
        return `"${classifyError(log.error)}","${log.model}","${log.provider}","${d.toLocaleDateString()}","${d.toLocaleTimeString()}","${details}"`;
    }).join('\n');
    const blob = new Blob([header + rows], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `bsl-error-report-${new Date().toISOString().slice(0,10)}.csv`;
    a.click(); URL.revokeObjectURL(url);
    showToast('Error report exported');
};

window.selfHealError = function(modelId, provider, errType) {
    triggerSelfHeal(modelId, provider, errType);
};

window.triggerSelfHeal = async function(targetModel, targetProvider, targetErrType) {
    const allErrors = logsDataState.filter(log => !!log.error || (log.status || 0) >= 400);
    if (allErrors.length === 0) { showToast('No errors to analyze', true); return; }
    
    const relevantErrors = targetModel
        ? allErrors.filter(e => e.model === targetModel && e.provider === targetProvider)
        : allErrors;
    
    // Self-healing logic: things the router CAN fix automatically
    const actions = [];
    
    relevantErrors.forEach(log => {
        const errMsg = (log.error || '').toLowerCase();
        const model = log.model;
        const prov = log.provider;
        
        // Auth errors → disable the model to prevent further failures
        if (errMsg.includes('401') || errMsg.includes('unauthorized') || errMsg.includes('403')) {
            const provData = globalConfig.providers?.[prov];
            if (provData) {
                const m = provData.models?.find(mm => mm.id === model);
                if (m && m.enabled !== false) {
                    m.enabled = false;
                    actions.push(`Disabled model "${model}" in provider "${prov}" (auth failure)`);
                }
            }
        }
        
        // Rate limit → disable temporarily (can re-enable manually)
        if (errMsg.includes('429') || errMsg.includes('rate limit')) {
            const provData = globalConfig.providers?.[prov];
            if (provData) {
                const m = provData.models?.find(mm => mm.id === model);
                if (m && m.enabled !== false) {
                    m.enabled = false;
                    actions.push(`Disabled model "${model}" in provider "${prov}" (rate limited — re-enable when quota resets)`);
                }
            }
        }
        
        // Model not found → disable it
        if (errMsg.includes('404') || errMsg.includes('not found') || errMsg.includes('does not exist')) {
            const provData = globalConfig.providers?.[prov];
            if (provData) {
                const m = provData.models?.find(mm => mm.id === model);
                if (m && m.enabled !== false) {
                    m.enabled = false;
                    actions.push(`Disabled model "${model}" in provider "${prov}" (model not found on endpoint)`);
                }
            }
        }
    });
    
    if (actions.length === 0) {
        // No locally-fixable issues — hand off to AI analysis
        showToast('No auto-fixable issues detected. Running AI analysis...', false);
        setTimeout(() => triggerErrorAnalysis(), 500);
        return;
    }
    
    await saveGlobalConfig();
    showToast(`Self-heal applied ${actions.length} fix(es). Reloading...`, false);
    setTimeout(() => { loadLogsData(); renderActiveTab(); }, 800);
    console.log('[Self-Heal] Actions taken:', actions);
};

async function triggerErrorAnalysis() {
    try {
        showToast("Starting error analysis...", "info");
        const res = await fetch('/api/observability/analyze_errors', { method: 'POST' });
        if (res.ok) {
            showToast("Analysis started. Please refresh logs in a few moments.", "success");
            setTimeout(loadLogsData, 5000); // auto refresh after 5s
        }
    } catch (e) {
        showToast("Failed to start analysis: " + e.message, "error");
    }
}

async function clearLogs() {
    if (!confirm("Are you sure you want to clear the console log?")) return;
    try {
        const res = await fetch('/api/observability/clear_logs', { method: 'POST' });
        if (res.ok) {
            showToast("Logs cleared.", "success");
            loadLogsData();
        }
    } catch (e) {
        showToast("Failed to clear logs: " + e.message, "error");
    }
}


document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', e => {
        e.preventDefault();
        currentView = 'list';
        isEditingVisibility = false;  // always exit visibility mode on tab switch
        document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
        item.classList.add('active');
        try { localStorage.setItem('bsl_active_tab', item.dataset.tab); } catch (e) { /* ignore */ }
        renderActiveTab();
    });
});

// Programmatically activate a tab by its data-tab name (used by in-page links
// like the Blacksand detail "Open Matrix" button).
window.selectTabByName = function(tabName) {
    const target = document.querySelector(`.nav-item[data-tab="${tabName}"]`);
    if (!target) return;
    currentView = 'list';
    isEditingVisibility = false;
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    target.classList.add('active');
    try { localStorage.setItem('bsl_active_tab', tabName); } catch (e) { /* ignore */ }
    renderActiveTab();
};

// Restore the last-active tab across reloads (F5/Ctrl-F5) so the panel no longer
// snaps back to Endpoint. Runs at load; renderActiveTab then honors the .active
// class set here instead of defaulting to the first nav item.
(function restoreActiveTab() {
    try {
        const saved = localStorage.getItem('bsl_active_tab');
        if (!saved) return;
        const target = document.querySelector(`.nav-item[data-tab="${saved}"]`);
        if (!target || target.offsetParent === null) return;  // skip missing/hidden tabs
        document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
        target.classList.add('active');
    } catch (e) { /* ignore */ }
})();

const _saveBtn = document.getElementById('save-btn');
if (_saveBtn) _saveBtn.addEventListener('click', saveConfig);

// ── Auth-gated initialization ──
// Check if admin password protection is enabled before loading config.
// If auth is required and no session exists, show login overlay and defer
// config loading until the user successfully authenticates.
(async () => {
    const authed = await checkAdminAuth();
    if (authed) {
        fetchConfig();
    }
    // If not authed, handleAdminLogin() will call fetchConfig() after success
})();

// ─── Auto Error Prevention — banner, push notifications, polling ─────────────

function requestPushPermission() {
    if (!('Notification' in window)) {
        showToast('This browser does not support desktop notifications', true);
        return;
    }
    if (Notification.permission === 'default') {
        Notification.requestPermission();
    }
}

const _pushedNotifIds = new Set();

function _sendDesktopPush(notif) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    if (_pushedNotifIds.has(notif.id)) return;
    _pushedNotifIds.add(notif.id);
    try {
        new Notification(notif.title || 'BSL Router', {
            body: notif.message || '',
            tag: 'bsl-ep-' + notif.id,
        });
    } catch (e) { /* ignore */ }
}

async function dismissEpNotification(id) {
    try {
        await fetch(`/api/error-prevention/notifications/${id}/dismiss`, { method: 'POST' });
    } catch (e) { /* ignore */ }
    pollErrorPrevention();
}

async function dismissAllEpNotifications() {
    try {
        await fetch('/api/error-prevention/notifications/dismiss-all', { method: 'POST' });
    } catch (e) { /* ignore */ }
    pollErrorPrevention();
}

const MAX_VISIBLE_BANNERS = 3;

function renderEpBanner(notifs) {
    const container = document.getElementById('ep-banner-container');
    if (!container) return;
    if (!notifs || notifs.length === 0) {
        container.innerHTML = '';
        return;
    }
    const colors = {
        info:     { bg: 'rgba(59,130,246,0.12)', border: '#3b82f6', icon: 'ℹ️' },
        warning:  { bg: 'rgba(249,115,22,0.12)', border: '#f97316', icon: '⚠️' },
        critical: { bg: 'rgba(239,68,68,0.14)',  border: '#ef4444', icon: '🚫' },
    };
    const visible = notifs.slice(0, MAX_VISIBLE_BANNERS);
    const overflow = notifs.length - visible.length;

    const rows = visible.map(n => {
        const c = colors[n.level] || colors.info;
        if (n.level === 'critical' && n.push) _sendDesktopPush(n);
        return `
            <div style="display:flex; align-items:flex-start; gap:12px; background:${c.bg}; border-left:4px solid ${c.border}; border-radius:8px; padding:12px 16px; margin-top:12px;">
                <span style="font-size:18px; line-height:1.2;">${c.icon}</span>
                <div style="flex:1; min-width:0;">
                    <div style="font-weight:600; font-size:14px; color:var(--text-main);">${n.title || ''}</div>
                    <div style="font-size:13px; color:var(--text-muted); margin-top:2px;">${n.message || ''}</div>
                </div>
                <button onclick="dismissEpNotification(${n.id})" title="Dismiss" style="background:none; border:none; cursor:pointer; color:var(--text-muted); font-size:18px; line-height:1; padding:0 4px;">×</button>
            </div>`;
    }).join('');

    let footerHtml = '';
    if (overflow > 0) {
        footerHtml = `
            <div style="text-align:center; margin-top:8px; font-size:12px; color:var(--text-muted);">
                +${overflow} more notification${overflow > 1 ? 's' : ''}
                &nbsp;·&nbsp;
                <button onclick="dismissAllEpNotifications()" style="background:none; border:none; cursor:pointer; color:var(--text-muted); font-size:12px; text-decoration:underline;">Dismiss all (${notifs.length})</button>
            </div>`;
    } else if (notifs.length > 1) {
        footerHtml = `<div style="text-align:right; margin-top:8px;"><button onclick="dismissAllEpNotifications()" style="background:none; border:none; cursor:pointer; color:var(--text-muted); font-size:12px; text-decoration:underline;">Dismiss all (${notifs.length})</button></div>`;
    }
    container.innerHTML = rows + footerHtml;
}
async function pollErrorPrevention() {
    try {
        const res = await fetch('/api/error-prevention/notifications');
        if (!res.ok) return;
        const notifs = await res.json();
        renderEpBanner(notifs);
    } catch (e) { /* silent — non-critical */ }
    // Refresh ban-state cache for the Error Report column
    try {
        const bRes = await fetch('/api/error-prevention/bans');
        if (bRes.ok) {
            const bans = await bRes.json();
            epBansState = {};
            bans.forEach(b => { epBansState[`${b.provider}/${b.model}`] = b; });
        }
    } catch (e) { /* silent */ }
}


function renderBanStatusCell(model, provider) {
    const b = epBansState[`${provider}/${model}`];
    if (!b) return '<span style="font-size:11px;color:var(--text-muted);">—</span>';
    if (b.ban_state === 'disabled') {
        return `<span style="background:#fee2e2;color:#dc2626;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;">Disabled</span>
                <button onclick="enableDisabledModel('${model}','${provider}')" style="margin-left:6px;font-size:11px;color:var(--brand-color);background:none;border:none;cursor:pointer;text-decoration:underline;">Enable</button>`;
    }
    const mins = Math.max(1, Math.ceil((b.remaining_seconds || 0) / 60));
    if (b.ban_state === 'softban') {
        return `<span style="background:#fef9c3;color:#ca8a04;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;">Softban (${mins}m left)</span>`;
    }
    if (b.ban_state === 'longban') {
        return `<span style="background:#ffedd5;color:#ea580c;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;">Longban (${mins}m left)</span>`;
    }
    return '<span style="font-size:11px;color:var(--text-muted);">—</span>';
}

async function clearAllBans() {
    // Resets temp (soft/long) bans only — disabled models stay disabled.
    try {
        const res = await fetch('/api/error-prevention/clear-bans', { method: 'POST' });
        const data = res.ok ? await res.json() : null;
        const n = data ? data.lifted_count : '?';
        showToast(`Temp bans reset (${n} cleared). Disabled models remain.`);
    } catch (e) {
        showToast('Failed to reset temp bans', true);
    }
    await pollErrorPrevention();
    if (typeof renderLogsView === 'function') renderLogsView();
}

async function liftAllBans() {
    if (!confirm('Lift All Bans will:\n• Clear ALL soft/long bans\n• Re-enable models disabled by Error Prevention (self-heal)\n\nManually disabled models stay disabled. This cannot be undone. Continue?')) return;
    try {
        const res = await fetch('/api/error-prevention/lift-all-bans', { method: 'POST' });
        const data = await res.json();
        if (data.ok) {
            showToast(
                `All bans lifted \u2014 temp cleared: ${data.temp_bans_lifted}, ` +
                `disabled states cleared: ${data.disabled_state_cleared || 0}, ` +
                `models re-enabled: ${data.models_reenabled}` +
                (data.disabled_remaining > 0 ? `, still disabled: ${data.disabled_remaining}` : '')
            );
            // Refresh config so re-enabled models appear immediately
            await fetchConfig();
        } else {
            showToast('Lift All Bans failed: ' + (data.error || 'unknown error'), true);
        }
    } catch (e) {
        showToast('Lift All Bans failed: ' + e.message, true);
    }
    await pollErrorPrevention();
    if (typeof renderLogsView === 'function') renderLogsView();
}

async function enableDisabledModel(model, provider) {
    try {
        const res = await fetch('/api/error-prevention/enable-model', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model, provider }),
        });
        if (res.ok) {
            showToast(`${model} re-enabled`);
            await fetchConfig();  // refresh local config (model.enabled flipped back)
        } else {
            showToast('Failed to re-enable model', true);
        }
    } catch (e) {
        showToast('Failed to re-enable model', true);
    }
    await pollErrorPrevention();
    if (typeof renderLogsView === 'function') renderLogsView();
}


// Initial poll + 30s interval
pollErrorPrevention();
setInterval(pollErrorPrevention, 30000);


function renderCombosTab() {
    const combos = globalConfig.combos || [];
    
    // Inject modal HTML if it doesn't exist
    if (!document.getElementById('combo-modal')) {
        document.body.insertAdjacentHTML('beforeend', `
            <div id="combo-modal" style="display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 1000; align-items: center; justify-content: center;">
                <div style="background: var(--bg-surface); width: 440px; border-radius: 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.1); overflow: hidden; display: flex; flex-direction: column;">
                    <div style="display: flex; justify-content: space-between; align-items: center; padding: 16px 24px; border-bottom: 1px solid var(--border-color);">
                        <div style="display: flex; gap: 6px; align-items: center;">
                            <div style="width: 12px; height: 12px; border-radius: 50%; background: #ff5f56;"></div>
                            <div style="width: 12px; height: 12px; border-radius: 50%; background: #ffbd2e;"></div>
                            <div style="width: 12px; height: 12px; border-radius: 50%; background: #27c93f;"></div>
                            <span id="combo-modal-title" style="margin-left: 12px; font-size: 14px; font-weight: 600; color: var(--text-main);">Create Combo</span>
                        </div>
                        <button class="btn" style="background: none; border: none; padding: 4px; color: var(--text-muted); cursor: pointer;" onclick="closeComboModal()">
                            <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                        </button>
                    </div>
                    <div style="padding: 24px; display: flex; flex-direction: column; gap: 16px;">
                        <input type="hidden" id="combo-modal-idx" value="-1">
                        <div>
                            <label style="display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; color: var(--text-main);">Combo Name</label>
                            <input type="text" id="combo-modal-alias" placeholder="e.g. coder-1" style="width: 100%; padding: 10px 12px; border: 1px solid var(--border-color); border-radius: 8px; font-size: 13px; outline: none; background: var(--bg-body); color: var(--text-main);">
                            <div style="font-size: 11px; color: var(--text-muted); margin-top: 4px;">Only letters, numbers, -, _ and . allowed</div>
                        </div>
                        
                        <div>
                            <label style="display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; color: var(--text-main);">Models</label>
                            <div style="border: 1px dashed var(--border-color); border-radius: 8px; padding: 12px; background: var(--bg-body);">
                                <div id="combo-modal-models-list" style="display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px;"></div>
                                
                                <button class="btn btn-outline" style="width: 100%; padding: 8px 12px; border-radius: 6px; color: var(--brand-color); border-color: var(--brand-color); display: flex; align-items: center; justify-content: center; gap: 6px;" onclick="openAddModelModal()">
                                    <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                                    Browse Models
                                </button>
                            </div>
                        </div>
                        
                        <div style="display: flex; justify-content: flex-end; gap: 12px; margin-top: 8px;">
                            <button class="btn" style="padding: 8px 16px; background: var(--bg-body); color: var(--text-main); border: 1px solid var(--border-color); border-radius: 8px; cursor: pointer;" onclick="closeComboModal()">Cancel</button>
                            <button class="btn btn-primary" style="padding: 8px 16px; border-radius: 8px;" onclick="saveComboModal()">Save</button>
                        </div>
                    </div>
                </div>
            </div>
        `);
    }

    // Inject add-model modal HTML if it doesn't exist
    if (!document.getElementById('add-model-modal')) {
        document.body.insertAdjacentHTML('beforeend', `
            <div id="add-model-modal" style="display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 1010; align-items: center; justify-content: center;">
                <div style="background: var(--bg-surface); width: 480px; border-radius: 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.1); overflow: hidden; display: flex; flex-direction: column; max-height: 80vh;">
                    <div style="display: flex; justify-content: space-between; align-items: center; padding: 16px 24px; border-bottom: 1px solid var(--border-color);">
                        <div style="display: flex; gap: 6px; align-items: center;">
                            <div style="width: 12px; height: 12px; border-radius: 50%; background: #ff5f56;"></div>
                            <div style="width: 12px; height: 12px; border-radius: 50%; background: #ffbd2e;"></div>
                            <div style="width: 12px; height: 12px; border-radius: 50%; background: #27c93f;"></div>
                            <span style="margin-left: 12px; font-size: 14px; font-weight: 600; color: var(--text-main);">Add Model to Combo</span>
                        </div>
                        <button class="btn" style="background: none; border: none; padding: 4px; color: var(--text-muted); cursor: pointer;" onclick="closeAddModelModal()">
                            <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                        </button>
                    </div>
                    
                    <div style="padding: 16px 24px; border-bottom: 1px solid var(--border-color);">
                        <div style="background: #fff5f5; border: 1px solid #fee2e2; border-radius: 8px; padding: 12px; display: flex; align-items: flex-start; gap: 8px; margin-bottom: 16px;">
                            <svg viewBox="0 0 24 24" width="16" height="16" stroke="var(--brand-color)" stroke-width="2" fill="none" style="margin-top: 2px;"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>
                            <span style="font-size: 12px; color: var(--text-main); line-height: 1.5;">Click to add, click again to remove. Changes are staged until you save the combo.</span>
                        </div>
                        
                        <div style="position: relative;">
                            <svg viewBox="0 0 24 24" width="16" height="16" stroke="var(--text-muted)" stroke-width="2" fill="none" style="position: absolute; left: 12px; top: 50%; transform: translateY(-50%);"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
                            <input type="text" id="add-model-search" placeholder="Search..." style="width: 100%; padding: 10px 12px 10px 36px; border: 1px solid var(--border-color); border-radius: 8px; font-size: 13px; outline: none; background: var(--bg-body); color: var(--text-main);" onkeyup="renderAddModelList()">
                        </div>
                    </div>
                    
                    <div id="add-model-list-container" style="padding: 16px 24px; overflow-y: auto; display: flex; flex-direction: column; gap: 20px;">
                        <!-- JS injected list here -->
                    </div>
                    
                    <div style="padding: 16px 24px; border-top: 1px solid var(--border-color); display: flex; justify-content: flex-end;">
                        <button class="btn btn-primary" style="padding: 8px 16px; border-radius: 8px;" onclick="closeAddModelModal()">Done</button>
                    </div>
                </div>
            </div>
        `);
    }

    return `
        <div class="combos-header" style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
            <div>
                <h2 style="font-size: 20px; font-weight: 600; margin: 0; display: flex; align-items: center; gap: 8px; color: var(--text-main);">
                    <svg viewBox="0 0 24 24" width="20" height="20" stroke="var(--brand-color)" stroke-width="2" fill="none"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>
                    Combos
                </h2>
                <div style="font-size: 14px; color: var(--text-muted); margin-top: 4px;">Model combos with fallback</div>
            </div>
            <button class="btn btn-primary" onclick="openComboModal(-1)" style="display: flex; align-items: center; gap: 6px; padding: 8px 16px; border-radius: 8px;">
                <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                Create Combo
            </button>
        </div>

        <div style="font-size: 13px; color: var(--text-muted); margin-bottom: 24px; line-height: 1.6; background: var(--bg-surface); padding: 16px; border-radius: 8px; border: 1px solid var(--border-color);">
            Group models under one name, then pick a strategy per combo:<br>
            <strong style="color: var(--text-main);">Fallback</strong> — tries models in order (next on failure)<br>
            <strong style="color: var(--text-main);">Round Robin</strong> — rotates models across requests to spread load
        </div>

        <div class="combos-list" style="display: flex; flex-direction: column; gap: 12px;">
            ${combos.map((c, idx) => `
                <div class="combo-card" style="display: flex; align-items: center; justify-content: space-between; padding: 16px; background: var(--bg-surface); border: 1px solid var(--border-color); border-radius: 12px;">
                    <div style="display: flex; align-items: center; gap: 16px; flex: 1;">
                        <div style="width: 40px; height: 40px; background: var(--brand-light); border-radius: 10px; display: flex; align-items: center; justify-content: center; color: var(--brand-color); border: 1px solid rgba(255, 95, 86, 0.2);">
                            <svg viewBox="0 0 24 24" width="20" height="20" stroke="currentColor" stroke-width="2" fill="none"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>
                        </div>
                        <div style="display: flex; flex-direction: column; gap: 4px;">
                            <div style="font-size: 15px; font-weight: 600; color: var(--text-main);">${c.alias}</div>
                            <div style="display: flex; flex-wrap: wrap; gap: 6px;">
                                ${c.chain.map(m => `<span style="font-size: 11px; background: var(--bg-body); color: var(--text-muted); padding: 2px 8px; border-radius: 12px; border: 1px solid var(--border-color);">${getComboModelDisplay(m)}</span>`).join('')}
                            </div>
                        </div>
                    </div>
                    
                    <div style="display: flex; align-items: center; gap: 16px;">
                        <select style="font-size: 13px; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border-color); background: var(--bg-body); outline: none; cursor: pointer; color: var(--text-main);" onchange="updateComboStrategy(${idx}, this.value)">
                            <option value="fallback" ${c.strategy !== 'round_robin' ? 'selected' : ''}>Fallback — try in order</option>
                            <option value="round_robin" ${c.strategy === 'round_robin' ? 'selected' : ''}>Round Robin — rotates models</option>
                        </select>
                        
                        <div style="display: flex; align-items: center; gap: 8px; padding-left: 12px; border-left: 1px solid var(--border-color);">
                            <button class="btn" style="background: transparent; color: var(--text-muted); padding: 4px 7px; display: flex; flex-direction: column; align-items: center; gap: 4px; font-size: 11px; border-radius: 6px; ${idx===0?'opacity:0.35;cursor:not-allowed;':'cursor:pointer;'}" onclick="moveComboListItem(${idx}, -1)" title="Move Combo Up" ${idx===0?'disabled':''}>
                                <span style="font-size:18px;line-height:14px;font-weight:700;">↑</span>
                                Up
                            </button>
                            <button class="btn" style="background: transparent; color: var(--text-muted); padding: 4px 7px; display: flex; flex-direction: column; align-items: center; gap: 4px; font-size: 11px; border-radius: 6px; ${idx===combos.length-1?'opacity:0.35;cursor:not-allowed;':'cursor:pointer;'}" onclick="moveComboListItem(${idx}, 1)" title="Move Combo Down" ${idx===combos.length-1?'disabled':''}>
                                <span style="font-size:18px;line-height:14px;font-weight:700;">↓</span>
                                Down
                            </button>
                            <button class="btn" style="background: transparent; color: var(--text-muted); padding: 4px 8px; display: flex; flex-direction: column; align-items: center; gap: 4px; font-size: 11px; border-radius: 6px;" onclick="copyComboAlias('${c.alias}')" title="Copy Alias">
                                <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                                Copy
                            </button>
                            <button class="btn" style="background: transparent; color: var(--text-muted); padding: 4px 8px; display: flex; flex-direction: column; align-items: center; gap: 4px; font-size: 11px; border-radius: 6px;" onclick="openComboModal(${idx})" title="Edit Combo">
                                <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>
                                Edit
                            </button>
                            <button class="btn" style="background: transparent; color: var(--danger); padding: 4px 8px; display: flex; flex-direction: column; align-items: center; gap: 4px; font-size: 11px; border-radius: 6px;" onclick="deleteCombo(${idx})" title="Delete Combo">
                                <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                                Delete
                            </button>
                        </div>
                    </div>
                </div>
            `).join('')}
            ${combos.length === 0 ? '<div style="font-size:14px;color:var(--text-muted);padding:32px 0;text-align:center;background:var(--bg-surface);border:1px dashed var(--border-color);border-radius:12px;">No combo models configured. Click "Create Combo" to get started.</div>' : ''}
        </div>
    `;
}

// ─── Combos JavaScript ───────────────────────────────────────────────────────

let tempComboModels = [];

function getProviderDisplayName(providerId) {
    const p = (globalConfig.providers || {})[providerId];
    return (p && p.name) ? p.name : providerId;
}

function parseComboModelRef(ref) {
    if (typeof ref === 'object' && ref !== null) {
        return {
            provider: ref.provider || null,
            model: ref.model || ref.id || '',
            thinking: ref.thinking || ref.thinking_config || null,
            raw: ref
        };
    }
    const raw = String(ref || '');
    const slash = raw.indexOf('/');
    if (slash > 0) {
        return { provider: raw.slice(0, slash), model: raw.slice(slash + 1), thinking: null, raw };
    }
    return { provider: null, model: raw, thinking: null, raw };
}

function makeComboModelRef(providerId, modelId) {
    return providerId ? `${providerId}/${modelId}` : modelId;
}

function getComboModelDisplay(ref) {
    const parsed = parseComboModelRef(ref);
    if (!parsed.provider) return parsed.model;
    return `${getProviderDisplayName(parsed.provider)} / ${parsed.model}`;
}

function comboRefEquals(a, b) {
    const pa = parseComboModelRef(a);
    const pb = parseComboModelRef(b);
    return pa.provider === pb.provider && pa.model === pb.model;
}

function findComboRefIndex(list, ref) {
    return list.findIndex(item => comboRefEquals(item, ref));
}

function getModelThinkingLabel(providerId, modelId) {
    const provider = (globalConfig.providers || {})[providerId];
    const model = provider && (provider.models || []).find(m => m.id === modelId);
    const t = model && (model.thinking || model.reasoning || model.thinking_config);
    if (!t) return 'thinking: default';
    if (typeof t === 'string') return `thinking: ${t}`;
    if (typeof t === 'object') return `thinking: ${t.mode || t.effort || t.budget || 'custom'}`;
    return `thinking: ${String(t)}`;
}


window.openComboModal = function(idx = -1) {
    const modal = document.getElementById('combo-modal');
    document.getElementById('combo-modal-idx').value = idx;
    
    if (idx >= 0) {
        const c = globalConfig.combos[idx];
        document.getElementById('combo-modal-title').innerText = 'Edit Combo';
        document.getElementById('combo-modal-alias').value = c.alias;
        tempComboModels = [...c.chain];
    } else {
        document.getElementById('combo-modal-title').innerText = 'Create Combo';
        document.getElementById('combo-modal-alias').value = '';
        tempComboModels = [];
    }
    
    renderComboModalModels();
    modal.style.display = 'flex';
};

window.closeComboModal = function() {
    document.getElementById('combo-modal').style.display = 'none';
};

window.renderComboModalModels = function() {
    const container = document.getElementById('combo-modal-models-list');
    if (tempComboModels.length === 0) {
        container.innerHTML = '<div style="font-size: 12px; color: var(--text-muted); text-align: center; padding: 12px 0;">No models added yet</div>';
        return;
    }

    container.innerHTML = tempComboModels.map((m, i) => {
        const parsed = parseComboModelRef(m);
        const displayName = getComboModelDisplay(m);
        const entryThinking = (typeof m === 'object' && m.thinking) ? m.thinking : (typeof m === 'string' && m.indexOf('|thinking:') !== -1 ? m.split('|thinking:')[1] : null);

        let thinkingHtml = '';
        if (parsed.provider) {
            const modelId = parsed.model;
            const spec = getThinkingSpec(modelId);
            if (spec && spec.alwaysOn) {
                thinkingHtml = `<span title="This model reasons on every request — no toggle needed" style="display:inline-flex;align-items:center;gap:4px;background:#ecfdf5;color:#047857;padding:2px 8px;border-radius:10px;font-weight:600;font-size:10px;border:1px solid #a7f3d0;white-space:nowrap;">✓ Always-on</span>`;
            } else {
                const opts = getThinkingOptionsForModel(modelId);
                if (opts.length > 0) {
                    thinkingHtml = `<select class="input" style="padding:3px 24px 3px 8px;width:auto;font-size:11px;border-radius:5px;background:#f9fafb;cursor:pointer;" onchange="updateComboModelThinking(${i}, this.value)">
                        <option value="auto" ${(!entryThinking || entryThinking === 'auto') ? 'selected' : ''}>Auto</option>
                        ${opts.map(opt => `<option value="${opt}" ${entryThinking === opt ? 'selected' : ''}>${opt}</option>`).join('')}
                    </select>`;
                } else {
                    thinkingHtml = `<span style="font-size:10px;color:var(--text-muted);">no thinking</span>`;
                }
            }
        } else {
            thinkingHtml = `<span style="font-size:10px;color:var(--text-muted);font-style:italic;">combo alias</span>`;
        }

        const providerLabel = parsed.provider ? getProviderDisplayName(parsed.provider) : '';
        const modelLabel = parsed.model || displayName;

        return `
        <div style="display:flex;align-items:center;justify-content:space-between;background:var(--bg-surface);padding:8px 10px;border-radius:8px;border:1px solid var(--border-color);gap:10px;min-height:48px;">
            <div style="display:flex;align-items:flex-start;gap:8px;flex:1;min-width:0;">
                <span style="font-size:11px;color:var(--text-muted);font-weight:700;flex-shrink:0;line-height:18px;">${i+1}</span>
                <div style="display:flex;flex-direction:column;gap:2px;min-width:0;flex:1;">
                    ${providerLabel ? `<span style="font-size:10px;color:var(--brand-color);font-weight:700;text-transform:uppercase;letter-spacing:0.25px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${providerLabel}</span>` : ''}
                    <span title="${modelLabel}" style="font-size:12px;color:var(--text-main);font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${modelLabel}</span>
                </div>
            </div>
            <div style="display:flex;align-items:center;gap:6px;flex-shrink:0;">
                ${thinkingHtml}
                <button class="btn btn-outline" title="Move up" aria-label="Move ${modelLabel} up" style="padding:3px 7px;font-size:11px;line-height:1;border-radius:6px;color:var(--text-muted);${i===0?'opacity:0.35;cursor:not-allowed;':'cursor:pointer;'}" onclick="moveComboModel(${i}, -1)" ${i===0?'disabled':''}>↑</button>
                <button class="btn btn-outline" title="Move down" aria-label="Move ${modelLabel} down" style="padding:3px 7px;font-size:11px;line-height:1;border-radius:6px;color:var(--text-muted);${i===tempComboModels.length-1?'opacity:0.35;cursor:not-allowed;':'cursor:pointer;'}" onclick="moveComboModel(${i}, 1)" ${i===tempComboModels.length-1?'disabled':''}>↓</button>
                <button class="btn" style="padding:3px;background:none;border:none;color:var(--danger);cursor:pointer;display:flex;align-items:center;margin-left:2px;" onclick="removeComboModel(${i})">
                    <svg viewBox="0 0 24 24" width="12" height="12" stroke="currentColor" stroke-width="2.5" fill="none"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                </button>
            </div>
        </div>`;
    }).join('');
};

function getThinkingOptionsForModel(modelId) {
    // Delegates to getThinkingSpec so the combo modal stays in sync with the Providers tab per-model reasoning spec (gpt-5.6 sol/terra/luna tiers, kimi always-on, grok mandatory).
    const spec = getThinkingSpec(modelId);
    if (!spec || spec.alwaysOn || !Array.isArray(spec.effort)) return [];
    return spec.effort;
}

window.updateComboModelThinking = function(idx, value) {
    const entry = tempComboModels[idx];
    if (typeof entry === 'string') {
        const parsed = parseComboModelRef(entry);
        tempComboModels[idx] = { provider: parsed.provider, model: parsed.model, thinking: value };
    } else if (typeof entry === 'object' && entry !== null) {
        entry.thinking = value;
    }
};

window.openAddModelModal = function() {
    const searchInput = document.getElementById('add-model-search');
    if (searchInput) searchInput.value = '';
    renderAddModelList();
    document.getElementById('add-model-modal').style.display = 'flex';
};

window.closeAddModelModal = function() {
    document.getElementById('add-model-modal').style.display = 'none';
};

window.toggleModelInCombo = function(modelRef) {
    const idx = findComboRefIndex(tempComboModels, modelRef);
    if (idx === -1) {
        tempComboModels.push(modelRef);
    } else {
        tempComboModels.splice(idx, 1);
    }
    renderComboModalModels();
    renderAddModelList();
};

window.renderAddModelList = function() {
    const search = (document.getElementById('add-model-search').value || '').toLowerCase().trim();
    const container = document.getElementById('add-model-list-container');
    if (!container) return;

    const groups = [];
    const currentAlias = document.getElementById('combo-modal-alias').value.trim();
    const currentIdx = parseInt(document.getElementById('combo-modal-idx').value, 10);

    const availableCombos = (globalConfig.combos || []).filter((c, i) => i !== currentIdx && c.alias !== currentAlias);
    if (availableCombos.length > 0) {
        groups.push({
            name: 'Combo Models',
            icon: '<svg viewBox="0 0 24 24" width="16" height="16" stroke="var(--brand-color)" stroke-width="2" fill="none"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>',
            models: availableCombos.map(c => ({ ref: c.alias, label: c.alias, sub: 'combo model' }))
        });
    }

    // BSL Models (Blacksand Labs) — right under the Combo group. Refs are BARE
    // ids so the backend dispatcher resolves them internally.
    const bslModels = _bslSelectableModels();
    if (bslModels.length > 0) {
        groups.push({
            name: 'BSL Models',
            icon: SVGS.blacksand,
            models: bslModels.map(m => ({ ref: m.id, label: m.name, sub: 'BSL model' }))
        });
    }

    for (const [provId, provData] of Object.entries(globalConfig.providers || {})) {
        if (_isBlacksandProvider(provId)) continue; // surfaced above under BSL Models
        if (!isProviderSelectable(provData)) continue;
        const enabledModels = (provData.models || []).filter(m => m && m.enabled !== false);
        if (enabledModels.length === 0) continue;
        groups.push({
            name: getProviderDisplayName(provId),
            icon: '<svg viewBox="0 0 24 24" width="16" height="16" stroke="var(--brand-color)" stroke-width="2" fill="none"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>',
            models: enabledModels.map(m => ({
                ref: makeComboModelRef(provId, m.id),
                label: m.id,
                sub: `${provId} · ${getModelThinkingLabel(provId, m.id)}`
            }))
        });
    }

    let html = '';
    let totalRendered = 0;

    for (const g of groups) {
        const filteredModels = g.models.filter(m => (`${m.label} ${m.sub || ''} ${g.name}`).toLowerCase().includes(search));
        if (filteredModels.length === 0) continue;
        totalRendered += filteredModels.length;
        html += `
            <div>
                <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px;">
                    ${g.icon}
                    <span style="font-size: 13px; font-weight: 600; color: var(--brand-color);">${g.name} <span style="color: var(--text-muted); font-size: 11px; font-weight: normal;">(${filteredModels.length})</span></span>
                </div>
                <div style="display: flex; flex-direction: column; gap: 4px;">
                    ${filteredModels.map(m => {
                        const isSelected = findComboRefIndex(tempComboModels, m.ref) !== -1;
                        const bg = isSelected ? 'var(--brand-light)' : 'var(--bg-body)';
                        const border = isSelected ? 'var(--brand-color)' : 'var(--border-color)';
                        const color = isSelected ? 'var(--brand-color)' : 'var(--text-main)';
                        const thinkingTag = (m.sub || '').split('·').pop().trim();
                        return `
                            <div style="padding:7px 12px;border-radius:8px;border:1px solid ${border};background:${bg};color:${color};font-size:12px;cursor:pointer;user-select:none;transition:all 0.15s;display:flex;align-items:center;justify-content:space-between;gap:8px;" onclick="toggleModelInCombo('${m.ref.replace(/'/g, "\\'")}')">
                                <span style="font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;font-size:13px;">${m.label}</span>
                                ${thinkingTag && thinkingTag !== 'combo model' ? `<span style="font-size:9px;color:var(--text-muted);white-space:nowrap;flex-shrink:0;background:var(--bg-surface);padding:1px 6px;border-radius:8px;border:1px solid var(--border-color);">${thinkingTag}</span>` : ''}
                            </div>
                        `;
                    }).join('')}
                </div>
            </div>
        `;
    }

    if (totalRendered === 0) {
        html = '<div style="text-align: center; color: var(--text-muted); font-size: 13px; padding: 20px 0;">No models match your search.</div>';
    }

    container.innerHTML = html;
};

window.removeComboModel = function(i) {
    tempComboModels.splice(i, 1);
    renderComboModalModels();
};

window.moveComboModel = function(i, dir) {
    if (i + dir < 0 || i + dir >= tempComboModels.length) return;
    const temp = tempComboModels[i];
    tempComboModels[i] = tempComboModels[i + dir];
    tempComboModels[i + dir] = temp;
    renderComboModalModels();
};

window.saveComboModal = async function() {
    const alias = document.getElementById('combo-modal-alias').value.trim();
    const idx = parseInt(document.getElementById('combo-modal-idx').value, 10);
    
    if (!alias) return showToast('Please provide a combo name', true);
    if (!/^[a-zA-Z0-9\-\_\.]+$/.test(alias)) return showToast('Invalid combo name', true);
    if (tempComboModels.length === 0) return showToast('Please add at least one model', true);
    
    if (!globalConfig.combos) globalConfig.combos = [];
    
    // Check duplicate alias
    const existingIdx = globalConfig.combos.findIndex(c => c.alias === alias);
    if (existingIdx !== -1 && existingIdx !== idx) {
        return showToast('Combo name already exists', true);
    }
    
    if (idx >= 0) {
        globalConfig.combos[idx].alias = alias;
        globalConfig.combos[idx].chain = [...tempComboModels];
    } else {
        globalConfig.combos.push({ alias, chain: [...tempComboModels], strategy: 'fallback' });
    }
    
    closeComboModal();
    await saveGlobalConfig();
    renderActiveTab();
    showToast(idx >= 0 ? 'Combo updated' : 'Combo created');
};

window.deleteCombo = async function(idx) {
    if (!confirm('Delete this combo model?')) return;
    globalConfig.combos.splice(idx, 1);
    await saveGlobalConfig();
    renderActiveTab();
    showToast('Combo deleted');
};

window.updateComboStrategy = async function(idx, strategy) {
    if (globalConfig.combos[idx]) {
        globalConfig.combos[idx].strategy = strategy;
        await saveGlobalConfig();
        showToast('Strategy updated');
    }
};

window.moveComboListItem = async function(idx, dir) {
    if (!Array.isArray(globalConfig.combos)) return;
    const nextIdx = idx + dir;
    if (idx < 0 || nextIdx < 0 || idx >= globalConfig.combos.length || nextIdx >= globalConfig.combos.length) return;
    const moved = globalConfig.combos[idx];
    globalConfig.combos[idx] = globalConfig.combos[nextIdx];
    globalConfig.combos[nextIdx] = moved;
    await saveGlobalConfig();
    renderActiveTab();
    showToast(`Combo moved ${dir < 0 ? 'up' : 'down'}`);
};

window.copyComboAlias = function(alias) {
    navigator.clipboard.writeText(alias).then(() => {
        showToast('Alias copied to clipboard');
    }).catch(() => {
        showToast('Failed to copy', true);
    });
};

// ─── Utility: Toast notification ─────────────────────────────────────────────
// Used throughout the app. Shows the #toast element with a message.
// isError: pass true or a truthy value to style as error (red text).
function showToast(message, isError) {
    const t = document.getElementById('toast');
    if (!t) return;
    t.textContent = message || 'Done';
    t.style.background = isError ? 'var(--danger, #ef4444)' : '';
    t.style.color = isError ? '#fff' : '';
    t.classList.add('show');
    clearTimeout(t._hideTimer);
    t._hideTimer = setTimeout(() => {
        t.classList.remove('show');
        t.style.background = '';
        t.style.color = '';
    }, 3000);
}

// ─── Utility: saveGlobalConfig ───────────────────────────────────────────────
// Alias used by combo functions. Wraps the main saveConfig() so combo
// save/delete don't need to know about the button UI.
async function saveGlobalConfig() {
    return saveConfig();
}


// ── Auto-Update System (9router-style) ────────────────────────────────────────

let _updateCheckDone = false;
let _updatePollInterval = null;

async function checkForUpdate() {
    if (_updateCheckDone) return;
    try {
        const res = await fetch('/api/version/check');
        const data = await res.json();
        _updateCheckDone = true;

        // Update version tag
        const versionTag = document.getElementById('version-tag');
        if (versionTag && data.currentVersion) {
            versionTag.textContent = 'v' + data.currentVersion;
        }

        // Show update pill if update available
        const pill = document.getElementById('update-pill');
        if (pill && data.hasUpdate && data.latestVersion) {
            document.getElementById('update-pill-text').textContent = '⬆ v' + data.latestVersion;
            pill.style.display = 'inline-flex';
        }
    } catch (e) {
        console.warn('[Update] Version check failed:', e);
    }
}

function startUpdate() {
    const modal = document.getElementById('update-modal');
    modal.style.display = 'flex';
    document.getElementById('update-modal-close').style.display = 'none';
    document.getElementById('update-modal-footer').style.display = 'none';
    document.getElementById('update-restart-btn').style.display = 'none';
    document.getElementById('update-close-btn').style.display = 'none';
    document.getElementById('update-error').style.display = 'none';
    document.getElementById('update-status-text').textContent = 'Starting update...';
    document.getElementById('update-progress-bar').style.width = '0%';
    document.getElementById('update-detail').textContent = '';

    // Trigger the update
    fetch('/api/version/update', { method: 'POST' })
        .then(res => res.json())
        .then(data => {
            if (data.error) {
                showUpdateError(data.error);
                return;
            }
            document.getElementById('update-status-text').textContent = 'Downloading update...';
            // Start polling for progress
            _updatePollInterval = setInterval(pollUpdateStatus, 1000);
        })
        .catch(e => showUpdateError(e.message));
}

function pollUpdateStatus() {
    fetch('/api/version/update/status')
        .then(res => res.json())
        .then(state => {
            const bar = document.getElementById('update-progress-bar');
            const statusText = document.getElementById('update-status-text');
            const detail = document.getElementById('update-detail');

            bar.style.width = (state.progress || 0) + '%';

            switch (state.phase) {
                case 'downloading':
                    statusText.textContent = 'Downloading update...';
                    detail.textContent = '';
                    break;
                case 'extracting':
                    statusText.textContent = 'Extracting files...';
                    detail.textContent = '';
                    break;
                case 'writing':
                    statusText.textContent = 'Applying update...';
                    detail.textContent = state.current_file
                        ? `${state.files_updated}/${state.files_total} files — ${state.current_file}`
                        : `${state.files_updated}/${state.files_total} files`;
                    break;
                case 'finalizing':
                    statusText.textContent = 'Finalizing...';
                    detail.textContent = '';
                    break;
                case 'done':
                    clearInterval(_updatePollInterval);
                    statusText.textContent = `✅ Update complete! ${state.files_updated} files updated, ${state.files_skipped} skipped.`;
                    detail.textContent = 'Restart BSL Router to apply changes.';
                    document.getElementById('update-modal-footer').style.display = 'flex';
                    document.getElementById('update-restart-btn').style.display = 'inline-flex';
                    document.getElementById('update-close-btn').style.display = 'inline-flex';
                    document.getElementById('update-modal-close').style.display = 'block';
                    break;
                case 'error':
                    clearInterval(_updatePollInterval);
                    showUpdateError(state.error || 'Unknown error');
                    break;
            }
        })
        .catch(() => {});
}

function showUpdateError(msg) {
    clearInterval(_updatePollInterval);
    document.getElementById('update-status-text').textContent = '❌ Update failed';
    document.getElementById('update-error').textContent = msg;
    document.getElementById('update-error').style.display = 'block';
    document.getElementById('update-modal-footer').style.display = 'flex';
    document.getElementById('update-close-btn').style.display = 'inline-flex';
    document.getElementById('update-modal-close').style.display = 'block';
}

function closeUpdateModal() {
    clearInterval(_updatePollInterval);
    document.getElementById('update-modal').style.display = 'none';
}

function restartServer() {
    document.getElementById('update-status-text').textContent = '🔄 Restarting BSL Router...';
    document.getElementById('update-restart-btn').style.display = 'none';
    document.getElementById('update-close-btn').style.display = 'none';

    fetch('/api/version/restart', { method: 'POST' })
        .then(res => res.json())
        .then(() => {
            // Wait for server to come back, then reload
            document.getElementById('update-detail').textContent = 'Waiting for server to restart...';
            setTimeout(() => {
                const checkInterval = setInterval(() => {
                    fetch('/api/version/check')
                        .then(res => {
                            if (res.ok) {
                                clearInterval(checkInterval);
                                location.reload();
                            }
                        })
                        .catch(() => {});
                }, 2000);
            }, 3000);
        })
        .catch(e => {
            document.getElementById('update-detail').textContent = 'Restart initiated. Please refresh the page in a few seconds.';
            setTimeout(() => location.reload(), 5000);
        });
}

// Auto-check for updates on page load
document.addEventListener('DOMContentLoaded', () => {
    setTimeout(checkForUpdate, 2000); // Delay 2s to not block initial page load
});
