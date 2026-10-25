// Объёмные «стеклянные» иконки показателей статистики (по образцу из дизайна): яркая синяя
// подложка + полупрозрачная матовая фигура с белым знаком. Встроенный SVG — работает офлайн.

const BLUE = "#1f5bff";
let seq = 0;

// Общие заготовки: градиент «матового стекла», блик и мягкая тень.
function defs(id) {
  return `<defs>
    <linearGradient id="g${id}" x1="0" y1="0" x2="0.9" y2="1">
      <stop offset="0" stop-color="#dfe8ff" stop-opacity="0.92"/>
      <stop offset="0.55" stop-color="#9fb8ff" stop-opacity="0.62"/>
      <stop offset="1" stop-color="#6f8fff" stop-opacity="0.55"/>
    </linearGradient>
    <linearGradient id="b${id}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#3b74ff"/><stop offset="1" stop-color="${BLUE}"/>
    </linearGradient>
    <filter id="s${id}" x="-30%" y="-30%" width="160%" height="160%">
      <feDropShadow dx="0" dy="2" stdDeviation="2" flood-color="#000a2e" flood-opacity="0.35"/>
    </filter>
  </defs>`;
}

const glass = (id) => `fill="url(#g${id})" stroke="rgba(255,255,255,0.55)" stroke-width="0.8" filter="url(#s${id})"`;
const blue = (id) => `fill="url(#b${id})"`;

const SHAPES = {
  // Сирена: синее основание, стеклянный купол, белая лампа, три луча.
  siren: (id) => `
    <g stroke="#cfdcff" stroke-opacity="0.85" stroke-width="3.6" stroke-linecap="round">
      <path d="M32 6v7"/><path d="M15 13l5 5"/><path d="M49 13l-5 5"/></g>
    <rect x="12" y="46" width="40" height="9" rx="3" ${blue(id)}/>
    <path d="M18 47V36a14 14 0 0 1 28 0v11z" ${glass(id)}/>
    <circle cx="32" cy="37" r="4.2" fill="#fff"/>`,
  // Колокол: синий круг сзади, стеклянный колокол, синий язычок.
  bell: (id) => `
    <circle cx="44" cy="20" r="12" ${blue(id)}/>
    <circle cx="32" cy="52" r="5.5" ${blue(id)}/>
    <path d="M32 9a3 3 0 0 1 3 3v1.2c7 1.5 11 7.4 11 14.8v8.5c0 3 2 5.5 4.5 7.5H14.5C17 42 19 39.5 19 36.5V28c0-7.4 4-13.3 10-14.8V12a3 3 0 0 1 3-3z" ${glass(id)}/>`,
  // Галочка: синий круг сзади, стеклянный круг, белая галочка.
  check: (id) => `
    <circle cx="42" cy="24" r="15" ${blue(id)}/>
    <circle cx="29" cy="35" r="18" ${glass(id)}/>
    <path d="M21 35.5l6 6 11-12" fill="none" stroke="#fff" stroke-width="4.4" stroke-linecap="round" stroke-linejoin="round"/>`,
  // Крестик: синий квадрат сзади, стеклянный квадрат, белый крест.
  x: (id) => `
    <rect x="22" y="10" width="32" height="32" rx="8" ${blue(id)}/>
    <rect x="10" y="20" width="34" height="34" rx="9" ${glass(id)}/>
    <path d="M21 31l12 12M33 31L21 43" stroke="#fff" stroke-width="4.4" stroke-linecap="round"/>`,
  // Секундомер: синий корпус-тень, кнопки, стеклянный циферблат, белые стрелки.
  clock: (id) => `
    <circle cx="36" cy="38" r="17" ${blue(id)}/>
    <rect x="25" y="5" width="14" height="6" rx="3" ${glass(id)}/>
    <rect x="45" y="13" width="6" height="5" rx="2" transform="rotate(40 48 15)" ${blue(id)}/>
    <circle cx="31" cy="37" r="18" ${glass(id)}/>
    <path d="M31 25v12l8 4.5" fill="none" stroke="#fff" stroke-width="3.8" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="31" cy="37" r="2.6" fill="#fff"/>`,
  // Сообщение с «!»: синяя карточка с текстом сзади, стеклянная карточка спереди.
  alert: (id) => `
    <rect x="26" y="8" width="30" height="24" rx="4" ${blue(id)}/>
    <g stroke="#9fb8ff" stroke-width="3" stroke-linecap="round" opacity="0.9"><path d="M33 17h16"/><path d="M33 24h10"/></g>
    <rect x="8" y="24" width="34" height="26" rx="5" ${glass(id)}/>
    <path d="M25 30v10" stroke="#fff" stroke-width="4" stroke-linecap="round"/>
    <circle cx="25" cy="45.5" r="2.4" fill="#fff"/>`,
};

export function kpiIcon(name) {
  const id = `k${++seq}`;
  const shape = SHAPES[name] || SHAPES.alert;
  return `<svg class="kpi-svg" viewBox="0 0 64 64" aria-hidden="true">${defs(id)}${shape(id)}</svg>`;
}
