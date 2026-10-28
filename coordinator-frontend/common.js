// Shared helpers for the two portals and the sign-in page.
// Sign-in (see Auth below) takes an email address or phone number, and a password. On the local dev server it is checked by /dev/sign-in
// and requests carry X-Dev-Identity; on a hosted build it goes to Cognito and requests carry the ID token.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const TT = { portal: null, user: null, identity: null, onSignIn: null };
const PORTALS = { staff: 'Staff portal', participant: 'Participant site' };
const PORTAL_NAMES = { ...PORTALS, home: 'Sign in' };

function store(k, v) {
  try { if (v !== undefined) localStorage.setItem('tt.' + k, v); return localStorage.getItem('tt.' + k); } catch { return null; }
}

// ------------------------------------------------------------ where the pages run
// LOCAL: the dev server (coordinator-backend/devserver) on this machine, with /api and /dev on the same origin.
// Otherwise a hosted build (Amplify or any static host) talking to what is deployed in AWS.
const LOCAL = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);
const CONFIG = {
  // Per portal. Staff: tt-portal-api (coordinator-backend/infra/deploy_portal.py). Participants: trialtalk-api,
  // from the CDK stack TrialTalk-Portal in account 180026596905.
  apiBase: LOCAL ? { staff: '/api', participant: '/api' }
    : { staff: 'https://ca0jle1s2g.execute-api.us-east-1.amazonaws.com',
        participant: 'https://ajda6ret3j.execute-api.us-east-1.amazonaws.com' },
  cognitoRegion: 'us-east-1',
  // Cognito app clients that allow USER_PASSWORD_AUTH. Staff: tt-staff-web in tt-staff, signing in by email.
  // Participants: the web client of trialtalk-participants, signing in by phone number (an alias in that pool).
  clients: { staff: '6tfennq5nb6hu8i50u2cvh3mo1', participant: '60qtjo1628btf8aibe7o0808pe' },
  // The default login the sign-in page fills in. Never put credentials in this file: it is served publicly. Locally,
  // index.html asks the dev server for it (GET /dev/demo); hosted there is none.
  demo: null,
};

// ------------------------------------------------------------ brand + icons
// The mark: a speech bubble (TrialTalk is a conversation) carrying a pulse line (it is about health).
const LOGO = `<svg viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="8" fill="var(--accent)"/>
  <path d="M7.5 10.5a3 3 0 0 1 3-3h11a3 3 0 0 1 3 3v8a3 3 0 0 1-3 3h-6.2l-4.3 3.6v-3.6h-.5a3 3 0 0 1-3-3z" fill="var(--accent-fg)"/>
  <path d="M10.5 14.6h2.6l1.5-3 2.4 6 1.5-3h2.9" fill="none" stroke="var(--accent)" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const BRAND = `${LOGO}<span class="word">Trial<b>Talk</b></span>`;

// Phosphor Icons 2.1 (MIT, vendor/phosphor/LICENSE): regular weight, plus fill for the current nav page.
const ICON_PATHS = {
  'pencil': '<path d="M227.31,73.37,182.63,28.68a16,16,0,0,0-22.63,0L36.69,152A15.86,15.86,0,0,0,32,163.31V208a16,16,0,0,0,16,16H92.69A15.86,15.86,0,0,0,104,219.31L227.31,96a16,16,0,0,0,0-22.63ZM92.69,208H48V163.31l88-88L180.69,120ZM192,108.68,147.31,64l24-24L216,84.68Z"/>',
  'overview': '<path d="M104,40H56A16,16,0,0,0,40,56v48a16,16,0,0,0,16,16h48a16,16,0,0,0,16-16V56A16,16,0,0,0,104,40Zm0,64H56V56h48v48Zm96-64H152a16,16,0,0,0-16,16v48a16,16,0,0,0,16,16h48a16,16,0,0,0,16-16V56A16,16,0,0,0,200,40Zm0,64H152V56h48v48Zm-96,32H56a16,16,0,0,0-16,16v48a16,16,0,0,0,16,16h48a16,16,0,0,0,16-16V152A16,16,0,0,0,104,136Zm0,64H56V152h48v48Zm96-64H152a16,16,0,0,0-16,16v48a16,16,0,0,0,16,16h48a16,16,0,0,0,16-16V152A16,16,0,0,0,200,136Zm0,64H152V152h48v48Z"/>',
  'alert': '<path d="M236.8,188.09,149.35,36.22h0a24.76,24.76,0,0,0-42.7,0L19.2,188.09a23.51,23.51,0,0,0,0,23.72A24.35,24.35,0,0,0,40.55,224h174.9a24.35,24.35,0,0,0,21.33-12.19A23.51,23.51,0,0,0,236.8,188.09ZM222.93,203.8a8.5,8.5,0,0,1-7.48,4.2H40.55a8.5,8.5,0,0,1-7.48-4.2,7.59,7.59,0,0,1,0-7.72L120.52,44.21a8.75,8.75,0,0,1,15,0l87.45,151.87A7.59,7.59,0,0,1,222.93,203.8ZM120,144V104a8,8,0,0,1,16,0v40a8,8,0,0,1-16,0Zm20,36a12,12,0,1,1-12-12A12,12,0,0,1,140,180Z"/>',
  'users': '<path d="M117.25,157.92a60,60,0,1,0-66.5,0A95.83,95.83,0,0,0,3.53,195.63a8,8,0,1,0,13.4,8.74,80,80,0,0,1,134.14,0,8,8,0,0,0,13.4-8.74A95.83,95.83,0,0,0,117.25,157.92ZM40,108a44,44,0,1,1,44,44A44.05,44.05,0,0,1,40,108Zm210.14,98.7a8,8,0,0,1-11.07-2.33A79.83,79.83,0,0,0,172,168a8,8,0,0,1,0-16,44,44,0,1,0-16.34-84.87,8,8,0,1,1-5.94-14.85,60,60,0,0,1,55.53,105.64,95.83,95.83,0,0,1,47.22,37.71A8,8,0,0,1,250.14,206.7Z"/>',
  'inbox': '<path d="M208,32H48A16,16,0,0,0,32,48V208a16,16,0,0,0,16,16H208a16,16,0,0,0,16-16V48A16,16,0,0,0,208,32Zm0,16V152h-28.7A15.86,15.86,0,0,0,168,156.69L148.69,176H107.31L88,156.69A15.86,15.86,0,0,0,76.69,152H48V48Zm0,160H48V168H76.69L96,187.31A15.86,15.86,0,0,0,107.31,192h41.38A15.86,15.86,0,0,0,160,187.31L179.31,168H208v40Z"/>',
  'team': '<path d="M200,112a8,8,0,0,1-8,8H152a8,8,0,0,1,0-16h40A8,8,0,0,1,200,112Zm-8,24H152a8,8,0,0,0,0,16h40a8,8,0,0,0,0-16Zm40-80V200a16,16,0,0,1-16,16H40a16,16,0,0,1-16-16V56A16,16,0,0,1,40,40H216A16,16,0,0,1,232,56ZM216,200V56H40V200H216Zm-80.26-34a8,8,0,1,1-15.5,4c-2.63-10.26-13.06-18-24.25-18s-21.61,7.74-24.25,18a8,8,0,1,1-15.5-4,39.84,39.84,0,0,1,17.19-23.34,32,32,0,1,1,45.12,0A39.76,39.76,0,0,1,135.75,166ZM96,136a16,16,0,1,0-16-16A16,16,0,0,0,96,136Z"/>',
  'usage': '<path d="M224,200h-8V40a8,8,0,0,0-8-8H152a8,8,0,0,0-8,8V80H96a8,8,0,0,0-8,8v40H48a8,8,0,0,0-8,8v64H32a8,8,0,0,0,0,16H224a8,8,0,0,0,0-16ZM160,48h40V200H160ZM104,96h40V200H104ZM56,144H88v56H56Z"/>',
  'activity': '<path d="M240,128a8,8,0,0,1-8,8H204.94l-37.78,75.58A8,8,0,0,1,160,216h-.4a8,8,0,0,1-7.08-5.14L95.35,60.76,63.28,131.31A8,8,0,0,1,56,136H24a8,8,0,0,1,0-16H50.85L88.72,36.69a8,8,0,0,1,14.76.46l57.51,151,31.85-63.71A8,8,0,0,1,200,120h32A8,8,0,0,1,240,128Z"/>',
  'phone': '<path d="M222.37,158.46l-47.11-21.11-.13-.06a16,16,0,0,0-15.17,1.4,8.12,8.12,0,0,0-.75.56L134.87,160c-15.42-7.49-31.34-23.29-38.83-38.51l20.78-24.71c.2-.25.39-.5.57-.77a16,16,0,0,0,1.32-15.06l0-.12L97.54,33.64a16,16,0,0,0-16.62-9.52A56.26,56.26,0,0,0,32,80c0,79.4,64.6,144,144,144a56.26,56.26,0,0,0,55.88-48.92A16,16,0,0,0,222.37,158.46ZM176,208A128.14,128.14,0,0,1,48,80,40.2,40.2,0,0,1,82.87,40a.61.61,0,0,0,0,.12l21,47L83.2,111.86a6.13,6.13,0,0,0-.57.77,16,16,0,0,0-1,15.7c9.06,18.53,27.73,37.06,46.46,46.11a16,16,0,0,0,15.75-1.14,8.44,8.44,0,0,0,.74-.56L168.89,152l47,21.05h0s.08,0,.11,0A40.21,40.21,0,0,1,176,208Z"/>',
  'plus': '<path d="M224,128a8,8,0,0,1-8,8H136v80a8,8,0,0,1-16,0V136H40a8,8,0,0,1,0-16h80V40a8,8,0,0,1,16,0v80h80A8,8,0,0,1,224,128Z"/>',
  'refresh': '<path d="M240,56v48a8,8,0,0,1-8,8H184a8,8,0,0,1,0-16H211.4L184.81,71.64l-.25-.24a80,80,0,1,0-1.67,114.78,8,8,0,0,1,11,11.63A95.44,95.44,0,0,1,128,224h-1.32A96,96,0,1,1,195.75,60L224,85.8V56a8,8,0,1,1,16,0Z"/>',
  'check': '<path d="M229.66,77.66l-128,128a8,8,0,0,1-11.32,0l-56-56a8,8,0,0,1,11.32-11.32L96,188.69,218.34,66.34a8,8,0,0,1,11.32,11.32Z"/>',
  'x': '<path d="M205.66,194.34a8,8,0,0,1-11.32,11.32L128,139.31,61.66,205.66a8,8,0,0,1-11.32-11.32L116.69,128,50.34,61.66A8,8,0,0,1,61.66,50.34L128,116.69l66.34-66.35a8,8,0,0,1,11.32,11.32L139.31,128Z"/>',
  'pill': '<path d="M216.42,39.6a53.26,53.26,0,0,0-75.32,0L39.6,141.09a53.26,53.26,0,0,0,75.32,75.31h0L216.43,114.91A53.31,53.31,0,0,0,216.42,39.6ZM103.61,205.09h0a37.26,37.26,0,0,1-52.7-52.69L96,107.31,148.7,160ZM205.11,103.6,160,148.69,107.32,96l45.1-45.09a37.26,37.26,0,0,1,52.69,52.69ZM189.68,82.34a8,8,0,0,1,0,11.32l-24,24a8,8,0,1,1-11.31-11.32l24-24A8,8,0,0,1,189.68,82.34Z"/>',
  'message': '<path d="M168,112a8,8,0,0,1-8,8H96a8,8,0,0,1,0-16h64A8,8,0,0,1,168,112Zm-8,24H96a8,8,0,0,0,0,16h64a8,8,0,0,0,0-16Zm72-8A104,104,0,0,1,79.12,219.82L45.07,231.17a16,16,0,0,1-20.24-20.24l11.35-34.05A104,104,0,1,1,232,128Zm-16,0A88,88,0,1,0,51.81,172.06a8,8,0,0,1,.66,6.54L40,216,77.4,203.53a7.85,7.85,0,0,1,2.53-.42,8,8,0,0,1,4,1.08A88,88,0,0,0,216,128Z"/>',
  'clock': '<path d="M128,24A104,104,0,1,0,232,128,104.11,104.11,0,0,0,128,24Zm0,192a88,88,0,1,1,88-88A88.1,88.1,0,0,1,128,216Zm64-88a8,8,0,0,1-8,8H128a8,8,0,0,1-8-8V72a8,8,0,0,1,16,0v48h48A8,8,0,0,1,192,128Z"/>',
  'user': '<path d="M230.92,212c-15.23-26.33-38.7-45.21-66.09-54.16a72,72,0,1,0-73.66,0C63.78,166.78,40.31,185.66,25.08,212a8,8,0,1,0,13.85,8c18.84-32.56,52.14-52,89.07-52s70.23,19.44,89.07,52a8,8,0,1,0,13.85-8ZM72,96a56,56,0,1,1,56,56A56.06,56.06,0,0,1,72,96Z"/>',
  'file': '<path d="M213.66,82.34l-56-56A8,8,0,0,0,152,24H56A16,16,0,0,0,40,40V216a16,16,0,0,0,16,16H200a16,16,0,0,0,16-16V88A8,8,0,0,0,213.66,82.34ZM160,51.31,188.69,80H160ZM200,216H56V40h88V88a8,8,0,0,0,8,8h48V216Zm-32-80a8,8,0,0,1-8,8H96a8,8,0,0,1,0-16h64A8,8,0,0,1,168,136Zm0,32a8,8,0,0,1-8,8H96a8,8,0,0,1,0-16h64A8,8,0,0,1,168,168Z"/>',
  'send': '<path d="M231.87,114l-168-95.89A16,16,0,0,0,40.92,37.34L71.55,128,40.92,218.67A16,16,0,0,0,56,240a16.15,16.15,0,0,0,7.93-2.1l167.92-96.05a16,16,0,0,0,.05-27.89ZM56,224a.56.56,0,0,0,0-.12L85.74,136H144a8,8,0,0,0,0-16H85.74L56.06,32.16A.46.46,0,0,0,56,32l168,95.83Z"/>',
  'siren': '<path d="M120,16V8a8,8,0,0,1,16,0v8a8,8,0,0,1-16,0Zm80,32a8,8,0,0,0,5.66-2.34l8-8a8,8,0,0,0-11.32-11.32l-8,8A8,8,0,0,0,200,48ZM50.34,45.66A8,8,0,0,0,61.66,34.34l-8-8A8,8,0,0,0,42.34,37.66Zm87,26.45a8,8,0,1,0-2.64,15.78C153.67,91.08,168,108.32,168,128a8,8,0,0,0,16,0C184,100.6,163.93,76.57,137.32,72.11ZM232,176v24a16,16,0,0,1-16,16H40a16,16,0,0,1-16-16V176a16,16,0,0,1,16-16V128a88,88,0,0,1,88.67-88c48.15.36,87.33,40.29,87.33,89v31A16,16,0,0,1,232,176ZM56,160H200V129c0-40-32.05-72.71-71.45-73H128a72,72,0,0,0-72,72Zm160,40V176H40v24H216Z"/>',
  'first-aid': '<path d="M216,56H176V48a24,24,0,0,0-24-24H104A24,24,0,0,0,80,48v8H40A16,16,0,0,0,24,72V200a16,16,0,0,0,16,16H216a16,16,0,0,0,16-16V72A16,16,0,0,0,216,56ZM96,48a8,8,0,0,1,8-8h48a8,8,0,0,1,8,8v8H96ZM216,200H40V72H216V200Zm-56-64a8,8,0,0,1-8,8H136v16a8,8,0,0,1-16,0V144H104a8,8,0,0,1,0-16h16V112a8,8,0,0,1,16,0v16h16A8,8,0,0,1,160,136Z"/>',
  'overview-fill': '<path d="M120,56v48a16,16,0,0,1-16,16H56a16,16,0,0,1-16-16V56A16,16,0,0,1,56,40h48A16,16,0,0,1,120,56Zm80-16H152a16,16,0,0,0-16,16v48a16,16,0,0,0,16,16h48a16,16,0,0,0,16-16V56A16,16,0,0,0,200,40Zm-96,96H56a16,16,0,0,0-16,16v48a16,16,0,0,0,16,16h48a16,16,0,0,0,16-16V152A16,16,0,0,0,104,136Zm96,0H152a16,16,0,0,0-16,16v48a16,16,0,0,0,16,16h48a16,16,0,0,0,16-16V152A16,16,0,0,0,200,136Z"/>',
  'alert-fill': '<path d="M236.8,188.09,149.35,36.22h0a24.76,24.76,0,0,0-42.7,0L19.2,188.09a23.51,23.51,0,0,0,0,23.72A24.35,24.35,0,0,0,40.55,224h174.9a24.35,24.35,0,0,0,21.33-12.19A23.51,23.51,0,0,0,236.8,188.09ZM120,104a8,8,0,0,1,16,0v40a8,8,0,0,1-16,0Zm8,88a12,12,0,1,1,12-12A12,12,0,0,1,128,192Z"/>',
  'users-fill': '<path d="M164.47,195.63a8,8,0,0,1-6.7,12.37H10.23a8,8,0,0,1-6.7-12.37,95.83,95.83,0,0,1,47.22-37.71,60,60,0,1,1,66.5,0A95.83,95.83,0,0,1,164.47,195.63Zm87.91-.15a95.87,95.87,0,0,0-47.13-37.56A60,60,0,0,0,144.7,54.59a4,4,0,0,0-1.33,6A75.83,75.83,0,0,1,147,150.53a4,4,0,0,0,1.07,5.53,112.32,112.32,0,0,1,29.85,30.83,23.92,23.92,0,0,1,3.65,16.47,4,4,0,0,0,3.95,4.64h60.3a8,8,0,0,0,7.73-5.93A8.22,8.22,0,0,0,252.38,195.48Z"/>',
  'inbox-fill': '<path d="M208,32H48A16,16,0,0,0,32,48V208a16,16,0,0,0,16,16H208a16,16,0,0,0,16-16V48A16,16,0,0,0,208,32Zm0,176H48V168H76.69L96,187.32A15.89,15.89,0,0,0,107.31,192h41.38A15.86,15.86,0,0,0,160,187.31L179.31,168H208v40Z"/>',
  'team-fill': '<path d="M112,120a16,16,0,1,1-16-16A16,16,0,0,1,112,120ZM232,56V200a16,16,0,0,1-16,16H40a16,16,0,0,1-16-16V56A16,16,0,0,1,40,40H216A16,16,0,0,1,232,56ZM135.75,166a39.76,39.76,0,0,0-17.19-23.34,32,32,0,1,0-45.12,0A39.84,39.84,0,0,0,56.25,166a8,8,0,0,0,15.5,4c2.64-10.25,13.06-18,24.25-18s21.62,7.73,24.25,18a8,8,0,1,0,15.5-4ZM200,144a8,8,0,0,0-8-8H152a8,8,0,0,0,0,16h40A8,8,0,0,0,200,144Zm0-32a8,8,0,0,0-8-8H152a8,8,0,0,0,0,16h40A8,8,0,0,0,200,112Z"/>',
  'usage-fill': '<path d="M232,208a8,8,0,0,1-8,8H32a8,8,0,0,1,0-16h8V136a8,8,0,0,1,8-8H72a8,8,0,0,1,8,8v64H96V88a8,8,0,0,1,8-8h32a8,8,0,0,1,8,8V200h16V40a8,8,0,0,1,8-8h40a8,8,0,0,1,8,8V200h8A8,8,0,0,1,232,208Z"/>',
  'activity-fill': '<path d="M216,40H40A16,16,0,0,0,24,56V200a16,16,0,0,0,16,16H216a16,16,0,0,0,16-16V56A16,16,0,0,0,216,40Zm-8,96H188.64L159,188a8,8,0,0,1-6.95,4h-.46a8,8,0,0,1-6.89-4.84L103,89.92,79,132a8,8,0,0,1-7,4H48a8,8,0,0,1,0-16H67.36L97.05,68a8,8,0,0,1,14.3.82L153,166.08l24-42.05a8,8,0,0,1,6.95-4h24a8,8,0,0,1,0,16Z"/>',
  'sun': '<path d="M120,40V16a8,8,0,0,1,16,0V40a8,8,0,0,1-16,0Zm72,88a64,64,0,1,1-64-64A64.07,64.07,0,0,1,192,128Zm-16,0a48,48,0,1,0-48,48A48.05,48.05,0,0,0,176,128ZM58.34,69.66A8,8,0,0,0,69.66,58.34l-16-16A8,8,0,0,0,42.34,53.66Zm0,116.68-16,16a8,8,0,0,0,11.32,11.32l16-16a8,8,0,0,0-11.32-11.32ZM192,72a8,8,0,0,0,5.66-2.34l16-16a8,8,0,0,0-11.32-11.32l-16,16A8,8,0,0,0,192,72Zm5.66,114.34a8,8,0,0,0-11.32,11.32l16,16a8,8,0,0,0,11.32-11.32ZM48,128a8,8,0,0,0-8-8H16a8,8,0,0,0,0,16H40A8,8,0,0,0,48,128Zm80,80a8,8,0,0,0-8,8v24a8,8,0,0,0,16,0V216A8,8,0,0,0,128,208Zm112-88H216a8,8,0,0,0,0,16h24a8,8,0,0,0,0-16Z"/>',
  'moon': '<path d="M233.54,142.23a8,8,0,0,0-8-2,88.08,88.08,0,0,1-109.8-109.8,8,8,0,0,0-10-10,104.84,104.84,0,0,0-52.91,37A104,104,0,0,0,136,224a103.09,103.09,0,0,0,62.52-20.88,104.84,104.84,0,0,0,37-52.91A8,8,0,0,0,233.54,142.23ZM188.9,190.34A88,88,0,0,1,65.66,67.11a89,89,0,0,1,31.4-26A106,106,0,0,0,96,56,104.11,104.11,0,0,0,200,160a106,106,0,0,0,14.92-1.06A89,89,0,0,1,188.9,190.34Z"/>',
  'monitor': '<path d="M208,40H48A24,24,0,0,0,24,64V176a24,24,0,0,0,24,24h72v16H96a8,8,0,0,0,0,16h64a8,8,0,0,0,0-16H136V200h72a24,24,0,0,0,24-24V64A24,24,0,0,0,208,40ZM48,56H208a8,8,0,0,1,8,8v80H40V64A8,8,0,0,1,48,56ZM208,184H48a8,8,0,0,1-8-8V160H216v16A8,8,0,0,1,208,184Z"/>',
  'envelope': '<path d="M224,48H32a8,8,0,0,0-8,8V192a16,16,0,0,0,16,16H216a16,16,0,0,0,16-16V56A8,8,0,0,0,224,48Zm-96,85.15L52.57,64H203.43ZM98.71,128,40,181.81V74.19Zm11.84,10.85,12,11.05a8,8,0,0,0,10.82,0l12-11.05,58,53.15H52.57ZM157.29,128,216,74.18V181.82Z"/>',
  'envelope-fill': '<path d="M224,48H32a8,8,0,0,0-8,8V192a16,16,0,0,0,16,16H216a16,16,0,0,0,16-16V56A8,8,0,0,0,224,48ZM98.71,128,40,181.81V74.19Zm11.84,10.85,12,11.05a8,8,0,0,0,10.82,0l12-11.05,58,53.15H52.57ZM157.29,128,216,74.18V181.82Z"/>',
  'pill-fill': '<path d="M216.43,39.6a53.27,53.27,0,0,0-75.33,0L39.6,141.09a53.26,53.26,0,0,0,75.32,75.31L216.43,114.91A53.32,53.32,0,0,0,216.43,39.6Zm-11.32,64-50.75,50.74-52.69-52.68,50.75-50.75a37.26,37.26,0,0,1,52.69,52.69ZM189.68,82.34a8,8,0,0,1,0,11.32l-24,24a8,8,0,1,1-11.31-11.32l24-24A8,8,0,0,1,189.68,82.34Z"/>',
  'message-fill': '<path d="M128,24A104,104,0,0,0,36.18,176.88L24.83,210.93a16,16,0,0,0,20.24,20.24l34.05-11.35A104,104,0,1,0,128,24Zm32,128H96a8,8,0,0,1,0-16h64a8,8,0,0,1,0,16Zm0-32H96a8,8,0,0,1,0-16h64a8,8,0,0,1,0,16Z"/>',
  'user-fill': '<path d="M230.93,220a8,8,0,0,1-6.93,4H32a8,8,0,0,1-6.92-12c15.23-26.33,38.7-45.21,66.09-54.16a72,72,0,1,1,73.66,0c27.39,8.95,50.86,27.83,66.09,54.16A8,8,0,0,1,230.93,220Z"/>',
  'file-fill': '<path d="M213.66,82.34l-56-56A8,8,0,0,0,152,24H56A16,16,0,0,0,40,40V216a16,16,0,0,0,16,16H200a16,16,0,0,0,16-16V88A8,8,0,0,0,213.66,82.34ZM160,176H96a8,8,0,0,1,0-16h64a8,8,0,0,1,0,16Zm0-32H96a8,8,0,0,1,0-16h64a8,8,0,0,1,0,16Zm-8-56V44l44,44Z"/>',
  'siren-fill': '<path d="M120,16V8a8,8,0,0,1,16,0v8a8,8,0,0,1-16,0Zm80,32a8,8,0,0,0,5.66-2.34l8-8a8,8,0,0,0-11.32-11.32l-8,8A8,8,0,0,0,200,48ZM50.34,45.66A8,8,0,0,0,61.66,34.34l-8-8A8,8,0,0,0,42.34,37.66ZM232,176v24a16,16,0,0,1-16,16H40a16,16,0,0,1-16-16V176a16,16,0,0,1,16-16V128a88,88,0,0,1,88.67-88c48.15.36,87.33,40.29,87.33,89v31A16,16,0,0,1,232,176ZM134.68,87.89C153.67,91.08,168,108.32,168,128a8,8,0,0,0,16,0c0-27.4-20.07-51.43-46.68-55.89a8,8,0,1,0-2.64,15.78ZM216,200V176H40v24H216Z"/>',
  'first-aid-fill': '<path d="M216,56H176V48a24,24,0,0,0-24-24H104A24,24,0,0,0,80,48v8H40A16,16,0,0,0,24,72V200a16,16,0,0,0,16,16H216a16,16,0,0,0,16-16V72A16,16,0,0,0,216,56Zm-64,88H136v16a8,8,0,0,1-16,0V144H104a8,8,0,0,1,0-16h16V112a8,8,0,0,1,16,0v16h16a8,8,0,0,1,0,16Zm8-88H96V48a8,8,0,0,1,8-8h48a8,8,0,0,1,8,8Z"/>',
  'caret-updown': '<path d="M181.66,170.34a8,8,0,0,1,0,11.32l-48,48a8,8,0,0,1-11.32,0l-48-48a8,8,0,0,1,11.32-11.32L128,212.69l42.34-42.35A8,8,0,0,1,181.66,170.34Zm-96-84.68L128,43.31l42.34,42.35a8,8,0,0,0,11.32-11.32l-48-48a8,8,0,0,0-11.32,0l-48,48A8,8,0,0,0,85.66,85.66Z"/>',
  'dots': '<path d="M140,128a12,12,0,1,1-12-12A12,12,0,0,1,140,128Zm56-12a12,12,0,1,0,12,12A12,12,0,0,0,196,116ZM60,116a12,12,0,1,0,12,12A12,12,0,0,0,60,116Z"/>',
  'sign-out': '<path d="M120,216a8,8,0,0,1-8,8H48a8,8,0,0,1-8-8V40a8,8,0,0,1,8-8h64a8,8,0,0,1,0,16H56V208h56A8,8,0,0,1,120,216Zm109.66-93.66-40-40a8,8,0,0,0-11.32,11.32L204.69,120H112a8,8,0,0,0,0,16h92.69l-26.35,26.34a8,8,0,0,0,11.32,11.32l40-40A8,8,0,0,0,229.66,122.34Z"/>',
  'undo': '<path d="M224,128a96,96,0,0,1-94.71,96H128A95.38,95.38,0,0,1,62.1,197.8a8,8,0,0,1,11-11.63A80,80,0,1,0,71.43,71.39a3.07,3.07,0,0,1-.26.25L44.59,96H72a8,8,0,0,1,0,16H24a8,8,0,0,1-8-8V56a8,8,0,0,1,16,0V85.8L60.25,60A96,96,0,0,1,224,128Z"/>',
  'sidebar': '<path d="M216,40H40A16,16,0,0,0,24,56V200a16,16,0,0,0,16,16H216a16,16,0,0,0,16-16V56A16,16,0,0,0,216,40ZM40,56H80V200H40ZM216,200H96V56H216V200Z"/>',
  'copy': '<path d="M216,32H88a8,8,0,0,0-8,8V80H40a8,8,0,0,0-8,8V216a8,8,0,0,0,8,8H168a8,8,0,0,0,8-8V176h40a8,8,0,0,0,8-8V40A8,8,0,0,0,216,32ZM160,208H48V96H160Zm48-48H176V88a8,8,0,0,0-8-8H96V48H208Z"/>',
  'home': '<path d="M219.31,108.68l-80-80a16,16,0,0,0-22.62,0l-80,80A15.87,15.87,0,0,0,32,120v96a8,8,0,0,0,8,8h64a8,8,0,0,0,8-8V160h32v56a8,8,0,0,0,8,8h64a8,8,0,0,0,8-8V120A15.87,15.87,0,0,0,219.31,108.68ZM208,208H160V152a8,8,0,0,0-8-8H104a8,8,0,0,0-8,8v56H48V120l80-80,80,80Z"/>',
  'expand': '<path d="M216,48V96a8,8,0,0,1-16,0V67.31l-50.34,50.35a8,8,0,0,1-11.32-11.32L188.69,56H160a8,8,0,0,1,0-16h48A8,8,0,0,1,216,48ZM106.34,138.34,56,188.69V160a8,8,0,0,0-16,0v48a8,8,0,0,0,8,8H96a8,8,0,0,0,0-16H67.31l50.35-50.34a8,8,0,0,0-11.32-11.32Z"/>',
  'collapse': '<path d="M213.66,53.66,163.31,104H192a8,8,0,0,1,0,16H144a8,8,0,0,1-8-8V64a8,8,0,0,1,16,0V92.69l50.34-50.35a8,8,0,0,1,11.32,11.32ZM112,136H64a8,8,0,0,0,0,16H92.69L42.34,202.34a8,8,0,0,0,11.32,11.32L104,163.31V192a8,8,0,0,0,16,0V144A8,8,0,0,0,112,136Z"/>',
  'chevron': '<path d="M181.66,133.66l-80,80a8,8,0,0,1-11.32-11.32L164.69,128,90.34,53.66a8,8,0,0,1,11.32-11.32l80,80A8,8,0,0,1,181.66,133.66Z"/>',
  'arrow': '<path d="M221.66,133.66l-72,72a8,8,0,0,1-11.32-11.32L196.69,136H40a8,8,0,0,1,0-16H196.69L138.34,61.66a8,8,0,0,1,11.32-11.32l72,72A8,8,0,0,1,221.66,133.66Z"/>',
  'search': '<path d="M229.66,218.34l-50.07-50.06a88.11,88.11,0,1,0-11.31,11.31l50.06,50.07a8,8,0,0,0,11.32-11.32ZM40,112a72,72,0,1,1,72,72A72.08,72.08,0,0,1,40,112Z"/>',
  'buildings': '<path d="M240,208H224V96a16,16,0,0,0-16-16H144V32a16,16,0,0,0-24.88-13.32L39.12,72A16,16,0,0,0,32,85.34V208H16a8,8,0,0,0,0,16H240a8,8,0,0,0,0-16ZM208,96V208H144V96ZM48,85.34,128,32V208H48ZM112,112v16a8,8,0,0,1-16,0V112a8,8,0,1,1,16,0Zm-32,0v16a8,8,0,0,1-16,0V112a8,8,0,1,1,16,0Zm0,56v16a8,8,0,0,1-16,0V168a8,8,0,0,1,16,0Zm32,0v16a8,8,0,0,1-16,0V168a8,8,0,0,1,16,0Z"/>',
  'buildings-fill': '<path d="M239.73,208H224V96a16,16,0,0,0-16-16H164a4,4,0,0,0-4,4V208H144V32.41a16.43,16.43,0,0,0-6.16-13,16,16,0,0,0-18.72-.69L39.12,72A16,16,0,0,0,32,85.34V208H16.27A8.18,8.18,0,0,0,8,215.47,8,8,0,0,0,16,224H240a8,8,0,0,0,8-8.53A8.18,8.18,0,0,0,239.73,208ZM76,184a8,8,0,0,1-8.53,8A8.18,8.18,0,0,1,60,183.72V168.27A8.19,8.19,0,0,1,67.47,160,8,8,0,0,1,76,168Zm0-56a8,8,0,0,1-8.53,8A8.19,8.19,0,0,1,60,127.72V112.27A8.19,8.19,0,0,1,67.47,104,8,8,0,0,1,76,112Zm40,56a8,8,0,0,1-8.53,8,8.18,8.18,0,0,1-7.47-8.26V168.27a8.19,8.19,0,0,1,7.47-8.26,8,8,0,0,1,8.53,8Zm0-56a8,8,0,0,1-8.53,8,8.19,8.19,0,0,1-7.47-8.26V112.27a8.19,8.19,0,0,1,7.47-8.26,8,8,0,0,1,8.53,8Z"/>',
  'chats': '<path d="M232.07,186.76a80,80,0,0,0-62.5-114.17A80,80,0,1,0,23.93,138.76l-7.27,24.71a16,16,0,0,0,19.87,19.87l24.71-7.27a80.39,80.39,0,0,0,25.18,7.35,80,80,0,0,0,108.34,40.65l24.71,7.27a16,16,0,0,0,19.87-19.86ZM62,159.5a8.28,8.28,0,0,0-2.26.32L32,168l8.17-27.76a8,8,0,0,0-.63-6,64,64,0,1,1,26.26,26.26A8,8,0,0,0,62,159.5Zm153.79,28.73L224,216l-27.76-8.17a8,8,0,0,0-6,.63,64.05,64.05,0,0,1-85.87-24.88A79.93,79.93,0,0,0,174.7,89.71a64,64,0,0,1,41.75,92.48A8,8,0,0,0,215.82,188.23Z"/>',
  'chats-fill': '<path d="M232.07,186.76a80,80,0,0,0-62.5-114.17A80,80,0,1,0,23.93,138.76l-7.27,24.71a16,16,0,0,0,19.87,19.87l24.71-7.27a80.39,80.39,0,0,0,25.18,7.35,80,80,0,0,0,108.34,40.65l24.71,7.27a16,16,0,0,0,19.87-19.86Zm-16.25,1.47L224,216l-27.76-8.17a8,8,0,0,0-6,.63,64.05,64.05,0,0,1-85.87-24.88A79.93,79.93,0,0,0,174.7,89.71a64,64,0,0,1,41.75,92.48A8,8,0,0,0,215.82,188.23Z"/>',
  'arrow-left': '<path d="M224,128a8,8,0,0,1-8,8H59.31l58.35,58.34a8,8,0,0,1-11.32,11.32l-72-72a8,8,0,0,1,0-11.32l72-72a8,8,0,0,1,11.32,11.32L59.31,120H216A8,8,0,0,1,224,128Z"/>',
  'home-fill': '<path d="M224,120v96a8,8,0,0,1-8,8H160a8,8,0,0,1-8-8V164a4,4,0,0,0-4-4H108a4,4,0,0,0-4,4v52a8,8,0,0,1-8,8H40a8,8,0,0,1-8-8V120a16,16,0,0,1,4.69-11.31l80-80a16,16,0,0,1,22.62,0l80,80A16,16,0,0,1,224,120Z"/>',
};

// ------------------------------------------------------------ theme: System (default), Light or Dark
// System leaves <html> unmarked so CSS light-dark() follows the OS; Light/Dark pin data-theme. Each page's <head>
// applies a saved choice before first paint, so there is no flash. The button lives in the top bar.
const THEMES = { system: ['monitor', 'System'], light: ['sun', 'Light'], dark: ['moon', 'Dark'] };
const Theme = {
  get() { const t = store('theme'); return THEMES[t] ? t : 'system'; },
  // System pins the OS's current scheme rather than leaving <html> unmarked: with color-scheme "light dark",
  // Chromium paints light-dark() backgrounds on visited links (every #/ tile and card) as a mix of both sides.
  apply() {
    const t = this.get();
    document.documentElement.dataset.theme = t === 'system' ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : t;
  },
  set(t) {
    store('theme', t);
    const swap = () => { this.apply(); renderThemeControls(); };
    if (document.startViewTransition && !reducedMotion()) document.startViewTransition(swap); else swap();
  },
};
const reducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
Theme.apply();   // pin the resolved scheme at load (the <head> script already pinned an explicit choice)
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (Theme.get() === 'system') Theme.apply(); });

// A card leaving a list: it fades first, then the gap closes, so the cards below slide up instead of jumping.
// Returns the animation (or null): cancel it after re-rendering, so a card that turns out to still be there reappears.
function collapseOut(el) {
  if (!el) return null;
  const cs = getComputedStyle(el), ease = getComputedStyle(document.documentElement).getPropertyValue('--ease-out').trim();
  el.style.overflow = 'hidden';
  // Easing per segment, not over the whole run: the fade and the close each get their own ease-out beat.
  const box = { height: `${el.offsetHeight}px`, paddingBlock: cs.paddingBlock, marginBlockStart: cs.marginBlockStart, borderWidth: cs.borderWidth };
  const frames = reducedMotion() ? [{ opacity: 1, easing: ease }, { opacity: 0 }]
    : [{ opacity: 1, ...box, easing: ease },
       { opacity: 0, ...box, offset: 0.4, easing: ease },
       { opacity: 0, height: '0px', paddingBlock: '0px', marginBlockStart: '0px', borderWidth: '0px' }];
  return el.animate(frames, { duration: 280, fill: 'forwards' });
}
function renderThemeControls() {
  const t = Theme.get(), [ic, label] = THEMES[t], order = Object.keys(THEMES), next = order[(order.indexOf(t) + 1) % order.length];
  $$('[data-theme-toggle]').forEach(el => {
    const hadFocus = el.contains(document.activeElement);
    el.innerHTML = `<button type="button" class="icon-btn" aria-label="Theme: ${label}. Switch to ${THEMES[next][1]}" title="Theme: ${label}">${icon(ic)}</button>`;
    $('button', el).onclick = () => Theme.set(next);
    if (hadFocus) $('button', el).focus();
  });
}

// ------------------------------------------------------------ the shared shell
// Every portal page: a sidebar that owns the brand, its own open/close button and the page navigation, and a top bar
// with breadcrumbs (where you are, and the way back), the portal switcher, the theme and the account menu.
// The landing page has no sidebar, so its brand sits in the top bar.
const PORTAL_TABS = [['staff', '/staff', 'users', 'Staff'], ['participant', '/participant', 'message', 'Participant']];

function renderChrome(portal) {
  const side = $('.sidebar'), h = $('#appHeader');
  if (side && !$('.sidebar-head', side)) {
    side.id = 'sidebar';
    side.insertAdjacentHTML('afterbegin', `<div class="sidebar-head">
      <a class="brand" href="/${portal}" aria-label="TrialTalk, ${esc(PORTAL_NAMES[portal])} home">${BRAND}</a>
      <button type="button" class="icon-btn nav-toggle" id="navToggle" aria-controls="sidebar">${icon('sidebar')}</button></div>`);
    $$('.nav a', side).forEach(a => { a.title = $('.nl', a)?.textContent || ''; });
  }
  if (h) h.innerHTML = `<div class="inner">
      ${side ? `<button type="button" class="icon-btn nav-open" id="navOpen" aria-controls="sidebar" aria-label="Open navigation" title="Open navigation">${icon('sidebar')}</button>
        <nav class="crumbs" aria-label="Breadcrumb"><ol id="crumbs"></ol></nav>`
        : `<a class="brand" href="/" aria-label="TrialTalk">${BRAND}</a>`}
      <span class="spacer"></span>
      ${LOCAL && PORTALS[portal] ? `<nav class="portal-tabs" aria-label="Portals">${PORTAL_TABS.map(([k, u, ic, label]) =>
        `<a href="${u}" ${k === portal ? 'aria-current="page"' : ''}>${icon(k === portal ? ic + '-fill' : ic)}<span>${label}</span></a>`).join('')}</nav>` : ''}
      <div data-theme-toggle></div>
      ${PORTALS[portal] ? `<button type="button" class="account" id="accountBtn" popovertarget="accountMenu"></button>
        <div popover id="accountMenu" class="menu account-menu" aria-label="Account"></div>` : ''}
    </div>`;
  if (side && !$('.scrim')) document.body.insertAdjacentHTML('beforeend', '<button type="button" class="scrim" tabindex="-1" aria-hidden="true"></button>');
  const f = $('#appFooter');
  if (f) f.innerHTML = LOCAL ? `<div class="inner"><span>TrialTalk · local development build</span><span>Synthetic data only</span></div>`
    : `<div class="inner"><span>TrialTalk</span><span>Questions about your account? Contact your study site.</span></div>`;
  Nav.init(); renderThemeControls(); renderAccount();
}

// Breadcrumbs: [label, href] pairs; the last one is the current page.
function setCrumbs(list) {
  const ol = $('#crumbs'); if (!ol) return;
  ol.innerHTML = list.map(([label, href], i) => i === list.length - 1
    ? `<li><span aria-current="page">${esc(label)}</span></li>`
    : `<li><a href="${esc(href)}">${esc(label)}</a>${icon('chevron', 'sm')}</li>`).join('');
}

// Sidebar: collapses to an icon rail on wide screens (remembered); on phones it is a drawer opened from the top bar.
const Nav = {
  mq: matchMedia('(max-width: 960px)'),
  init() {
    const t = $('#navToggle'); if (!t) return;
    t.onclick = () => this.mq.matches ? this.drawer(false) : this.collapse(!this.collapsed());
    $('#navOpen').onclick = () => this.drawer(true);
    $('.scrim').onclick = () => this.drawer(false);
    $$('.sidebar .nav a').forEach(a => a.addEventListener('click', () => this.mq.matches && this.drawer(false)));
    addEventListener('keydown', e => { if (e.key === 'Escape' && document.documentElement.classList.contains('nav-open')) this.drawer(false); });
    this.mq.addEventListener('change', () => { this.drawer(false, true); this.sync(); });
    this.collapse(this.collapsed());
  },
  collapsed() { return store('nav.collapsed') === '1'; },
  collapse(on) {
    store('nav.collapsed', on ? '1' : '0');
    document.documentElement.classList.toggle('nav-collapsed', on);
    this.sync();
  },
  drawer(open, quiet) {
    const was = document.documentElement.classList.contains('nav-open');
    document.documentElement.classList.toggle('nav-open', open);
    this.sync();
    if (open) ($('.sidebar .nav a[aria-current]') || $('.sidebar .nav a'))?.focus();   // start on the current page
    else if (was && !quiet) $('#navOpen')?.focus();
  },
  sync() {
    const t = $('#navToggle'); if (!t) return;
    const phone = this.mq.matches, drawerOpen = document.documentElement.classList.contains('nav-open');
    $('.sidebar')?.toggleAttribute('inert', phone && !drawerOpen);
    $('#navOpen')?.setAttribute('aria-expanded', phone && drawerOpen);
    const label = phone ? 'Close navigation' : this.collapsed() ? 'Expand sidebar' : 'Collapse sidebar';
    t.setAttribute('aria-expanded', phone ? drawerOpen : !this.collapsed());
    t.setAttribute('aria-label', label); t.title = label;
  },
};

// Kobra-style trigger: avatar, name, up/down caret. Signed out it reads "Sign in".
function renderAccount() {
  const b = $('#accountBtn'); if (!b) return;
  const u = TT.identity, name = u ? (u.full_name || u.name || u.username) : null;
  b.innerHTML = name ? `<span class="avatar" aria-hidden="true">${esc(initials(name))}</span><span class="name">${esc(name)}</span>${icon('caret-updown', 'sm')}`
    : `${icon('user', 'sm')}<span class="name">Sign in</span>${icon('caret-updown', 'sm')}`;
  b.setAttribute('aria-label', name ? `Signed in as ${name}. Account and settings` : 'Sign in');
  b.title = name ? `${name} · ${identitySub(u)}` : 'Sign in';
  renderAccountMenu();
}

function identitySub(u) {
  if (!u) return '';
  if (TT.portal === 'participant') return `${u.username}${u.status ? ' · ' + humanize(u.status) : ''}`;
  const role = (u.groups || []).map(g => g === 'PI' ? 'PI' : humanize(g)).join(' + ') || 'No role';
  const sites = String(u.site_ids || '').split(',').filter(Boolean).map(s => humanize(s.toLowerCase())).join(', ');
  return [role, sites].filter(Boolean).join(' · ');
}

// Hosted: who you are and Sign out. Local: also switch between the dev server's accounts and reload test data.
function renderAccountMenu() {
  const m = $('#accountMenu'); if (!m) return;
  const signOut = `<button type="button" class="menu-item" data-sign-out>${icon('sign-out')}<span class="mi-text"><b>Sign out</b></span></button>`;
  if (!LOCAL) {
    const u = TT.identity, name = u ? (u.full_name || u.name || u.username) : '';
    m.innerHTML = `<div class="menu-who"><span class="avatar" aria-hidden="true">${esc(initials(name))}</span>
      <span class="mi-text"><b>${esc(name)}</b><span>${esc(identitySub(u))}</span></span></div><div class="menu-sep"></div>${signOut}`;
    $('[data-sign-out]', m).onclick = () => Auth.signOut();
    return;
  }
  const list = TT.identities || [], set = loadedDataset();
  m.innerHTML = `
    <div class="menu-label">Switch account<span class="tag">Local dev</span></div>
    <div class="menu-list">${list.map(u => {
      const name = u.full_name || u.name || u.username, on = u.username === TT.user, off = u.enabled === false;
      return `<button type="button" class="menu-item" data-as="${esc(u.username)}" aria-pressed="${on}" ${off ? 'aria-disabled="true"' : ''}>
        <span class="avatar" aria-hidden="true">${esc(initials(name))}</span>
        <span class="mi-text"><b>${esc(name)}</b><span>${esc(identitySub(u))}</span></span>
        ${off ? chip('disabled') : on ? icon('check', 'sm mi-check') : ''}</button>`;
    }).join('')}</div>
    ${signOut}
    <div class="menu-sep"></div>
    <div class="menu-label">Test data</div>
    <div class="menu-pad"><div class="tabs full" role="group" aria-label="Test data">
      <button type="button" data-dataset="demo" aria-pressed="${set === 'demo'}">Demo data</button>
      <button type="button" data-dataset="worst" aria-pressed="${set === 'worst'}">Worst case</button></div></div>`;
  $$('[data-as]', m).forEach(b => {
    b.onclick = () => {
      if (b.getAttribute('aria-disabled') === 'true') return toast('This account is disabled. Enable it on the Team page first.', true);
      m.hidePopover(); signInAs(b.dataset.as);
    };
  });
  $('[data-sign-out]', m).onclick = () => Auth.signOut();
  $$('[data-dataset]', m).forEach(b => { b.onclick = () => { m.hidePopover(); switchDataset(b.dataset.dataset); }; });
}

// Which test data is loaded, read from the data itself (a saved flag goes stale when the dev server restarts):
// only the worst case has the northwind staff invite or a participant named "Jo".
function loadedDataset() {
  return (TT.identities || []).some(u => /northwind-industries/.test(u.username || '') || u.full_name === 'Jo') ? 'worst' : 'demo';
}

function signInAs(username) {
  TT.user = username;
  TT.identity = (TT.identities || []).find(u => u.username === username) || null;
  Auth.save({ portal: TT.portal, user: username });
  renderAccount(); signedIn();
  $('#accountBtn')?.focus();
}

// ------------------------------------------------------------ copy to clipboard
// Long values (emails) are truncated in place with the full value in a tooltip and a copy button beside them.
function copyable(text, label = 'email') {
  if (!text) return '<span class="muted">—</span>';
  return `<span class="copyable"><span class="trunc" title="${esc(text)}">${esc(text)}</span><button type="button" class="icon-btn xs" data-copy="${esc(text)}"
    aria-label="Copy ${esc(label)}" title="Copy ${esc(label)}">${icon('copy', 'sm')}${icon('check', 'sm done')}</button></span>`;
}
document.addEventListener('click', async e => {
  const b = e.target.closest('[data-copy]'); if (!b) return;
  e.stopPropagation();
  try { await navigator.clipboard.writeText(b.dataset.copy); } catch {
    const t = Object.assign(document.createElement('textarea'), { value: b.dataset.copy }); document.body.append(t); t.select(); document.execCommand('copy'); t.remove();
  }
  b.classList.add('copied'); clearTimeout(b._t); b._t = setTimeout(() => b.classList.remove('copied'), 1600);
  toast('Copied to clipboard');
});

// ------------------------------------------------------------ menus: native popovers placed under their trigger
// Light dismiss, Escape and top-layer stacking come from the platform; this places the panel and adds arrow keys.
function placeMenu(menu) {
  const trigger = $(`[popovertarget="${menu.id}"]`); if (!trigger) return;
  const r = trigger.getBoundingClientRect(), w = menu.offsetWidth, h = menu.offsetHeight;
  const left = Math.max(8, Math.min(r.right - w, innerWidth - w - 8));
  const below = r.bottom + 6, top = below + h > innerHeight - 8 && r.top - h - 6 > 8 ? r.top - h - 6 : below;
  menu.style.left = left + 'px'; menu.style.top = Math.max(8, top) + 'px';
}
document.addEventListener('toggle', e => {
  const m = e.target;
  if (!(m instanceof HTMLElement) || !m.classList.contains('menu') || e.newState !== 'open') return;
  placeMenu(m);
  ($('[aria-pressed="true"].menu-item', m) || $('button:not([aria-disabled="true"])', m))?.focus();
}, true);
document.addEventListener('keydown', e => {
  const m = e.target.closest?.('.menu');
  if (!m || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) return;
  const items = $$('button', m), i = items.indexOf(e.target);
  const next = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: items.length - 1 }[e.key];
  items[(next + items.length) % items.length]?.focus(); e.preventDefault();
});
addEventListener('resize', () => $$('.menu:popover-open').forEach(placeMenu));

const icon = (name, cls = '') => `<svg class="icon ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;

function injectAssets() {
  document.body.insertAdjacentHTML('afterbegin', `<svg width="0" height="0" style="position:absolute" aria-hidden="true">${
    Object.entries(ICON_PATHS).map(([k, d]) => `<symbol id="i-${k}" viewBox="0 0 256 256">${d}</symbol>`).join('')}</svg>`);
  $$('[data-brand]').forEach(el => { el.innerHTML = BRAND; });
  $$('[data-icon]').forEach(el => { el.outerHTML = icon(el.dataset.icon, el.className); });
  const fav = document.createElement('link');
  fav.rel = 'icon';
  fav.href = 'data:image/svg+xml,' + encodeURIComponent(LOGO.replace(/var\(--accent\)/g, '#036f4f').replace(/var\(--accent-fg\)/g, '#fff'));
  document.head.append(fav);
  if (!$('#toast')) document.body.insertAdjacentHTML('beforeend', '<div id="toast" popover="manual" aria-live="polite"></div>');
  if (!$('#dlg')) document.body.insertAdjacentHTML('beforeend', '<dialog id="dlg"></dialog>');
}

// ------------------------------------------------------------ feedback: toast and dialogs
// Routine confirmations disappear after 5 s; errors and emergencies stay until dismissed.
function toast(msg, bad) {
  const t = $('#toast');
  clearTimeout(t._h);
  t.className = bad ? 'bad' : '';
  t.setAttribute('aria-live', bad ? 'assertive' : 'polite');
  t.innerHTML = `${icon(bad ? 'alert' : 'check', 'ti')}<span class="msg">${esc(msg)}</span><button type="button" aria-label="Dismiss">${icon('x', 'sm')}</button>`;
  // Re-show so it lands on top of whatever is in the top layer now (an open dialog or card).
  if (t.matches(':popover-open')) t.hidePopover();
  t.showPopover();
  $('button', t).onclick = () => t.hidePopover();
  if (!bad) t._h = setTimeout(() => t.hidePopover(), 4000);
}

// In-page replacement for prompt()/confirm(): a real modal with a visible label, inline validation and a
// verb-first confirm button. Resolves to the input value (or true without an input), or null on cancel.
function ask({ title, message = '', input = null, confirm = 'Confirm', tone = 'primary', cancel = 'Cancel', pre = '' }) {
  return new Promise(resolve => {
    const d = $('#dlg');
    let result = null;
    const field = !input ? '' : input.multiline
      ? `<textarea name="v" rows="4" placeholder="${esc(input.placeholder || '')}" aria-describedby="dlgErr">${esc(input.value || '')}</textarea>`
      : `<input name="v" type="${esc(input.type || 'text')}" value="${esc(input.value || '')}" placeholder="${esc(input.placeholder || '')}" aria-describedby="dlgErr" autocomplete="off">`;
    d.innerHTML = `<form method="dialog">
      <h2 id="dlgTitle">${esc(title)}</h2>
      ${message ? `<p class="msg">${esc(message)}</p>` : ''}
      ${pre ? `<pre>${esc(pre)}</pre>` : ''}
      ${input ? `<label>${esc(input.label)}${input.required ? '' : ' <span class="hint">(optional)</span>'}${field}</label>
        <p class="err" id="dlgErr" hidden>${esc(input.error || 'This field is required.')}</p>` : ''}
      <div class="actions">${cancel ? `<button type="submit" value="cancel" formnovalidate>${esc(cancel)}</button>` : ''}
        <button type="submit" value="ok" class="${esc(tone)}">${esc(confirm)}</button></div></form>`;
    d.setAttribute('aria-labelledby', 'dlgTitle');
    const f = $('form', d);
    f.onsubmit = e => {
      const ok = e.submitter?.value === 'ok';
      if (ok && input?.required && !f.v.value.trim()) {
        e.preventDefault();
        f.v.setAttribute('aria-invalid', 'true'); $('#dlgErr').hidden = false; f.v.focus();
        return;
      }
      result = ok ? (input ? f.v.value.trim() : true) : null;
    };
    d.onclose = () => resolve(result);
    d.showModal();
    (f.v || $('button[value="ok"]', f)).focus();
  });
}

// ------------------------------------------------------------ API
// A value inside a URL path, such as a staff username (an email). "@" and "+" are legal in a path as they are, and the
// backend router matches the raw path without decoding it, so they must not be percent-encoded.
const pathSeg = v => encodeURIComponent(v).replace(/%40/g, '@').replace(/%2B/g, '+');
// opts.quiet: background polling. No error toast, so widgets can refresh freely.
async function api(method, path, body, opts = {}) {
  if (!TT.user) { if (!opts.quiet) toast('Sign in first.', true); throw new Error('signed out'); }
  const headers = await Auth.headers();
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch(CONFIG.apiBase[TT.portal] + path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !LOCAL) { Auth.signOut('expired'); throw new Error('signed out'); }
  if (!res.ok) {
    if (!opts.quiet) toast(`Unable to complete that (${res.status}). ${data.error || data.message || 'Try again.'}`, true);
    throw Object.assign(new Error(data.error || 'Request failed'), { status: res.status });
  }
  return data;
}

async function dev(method, path) {
  const res = await fetch(path, { method });
  const data = await res.json();
  if (!res.ok) { toast(data.error || 'Unable to complete that. Try again.', true); throw new Error(data.error); }
  return data;
}

// ------------------------------------------------------------ status: one tone per meaning
// Which tone a status gets is a clinical judgement, not a styling one: "withdrawn" is a normal end of
// participation (neutral), not an error; "open" alerts are the only red, so red always means "act now".
const STATUS_TONE = {
  prospect: 'neutral', screening: 'info', enrolled: 'ok', screen_failed: 'warn', withdrawn: 'neutral',
  active: 'ok', paused: 'warn', draft: 'neutral', cancelled: 'neutral', completed: 'info',
  open: 'bad', acknowledged: 'warn', resolved: 'ok',
  enabled: 'ok', disabled: 'neutral',
  new: 'warn', answered: 'ok',
  taken: 'ok', skipped: 'warn', missed: 'bad', snoozed: 'info', pending: 'neutral', no_response: 'neutral',
  ENABLED: 'ok', DISABLED: 'neutral',  // EventBridge schedule states
};
const STATUS_WORDS = new Set(Object.keys(STATUS_TONE));
const humanize = s => { let t = String(s ?? '').replace(/_/g, ' ').trim(); if (t.length > 1 && t === t.toUpperCase()) t = t.toLowerCase();
  return t.charAt(0).toUpperCase() + t.slice(1); };

function chip(status, label) {
  if (status === null || status === undefined || status === '') return '';
  return `<span class="pill tone-${STATUS_TONE[status] || 'neutral'}"><span class="dot"></span>${esc(label || humanize(status))}</span>`;
}

// ------------------------------------------------------------ rendering
const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/;
// The database runs in UTC and returns "2026-10-27 19:12:23.71" with no zone, so a bare timestamp means UTC.
function parseTs(v) {
  const s = String(v || '').replace(' ', 'T');
  return new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(s) ? s : s + 'Z');
}
function when(v) {
  const m = DATE_RE.exec(String(v || ''));
  if (!m) return String(v ?? '');
  const d = m[4] ? parseTs(v) : new Date(+m[1], +m[2] - 1, +m[3]);
  if (isNaN(d)) return String(v);
  const opts = { month: 'short', day: 'numeric' };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
  if (m[4]) Object.assign(opts, { hour: 'numeric', minute: '2-digit' });
  return new Intl.DateTimeFormat(undefined, opts).format(d);
}

function fmt(v) {
  if (v === null || v === undefined || v === '') return '<span class="muted">—</span>';
  if (typeof v === 'boolean' || v === 'true' || v === 'false') return v === true || v === 'true' ? 'Yes' : 'No';
  if (typeof v === 'object') return esc(JSON.stringify(v));
  if (typeof v === 'string' && DATE_RE.test(v)) return `<time datetime="${esc(v)}" title="${esc(v)}">${esc(when(v))}</time>`;
  return esc(v);
}

// Column keys come straight from the API; people read headers.
const ENUM_COLS = new Set(['record_type', 'source', 'response', 'channel', 'direction', 'msg_type', 'delivery_status', 'type', 'state']);
const COL_LABELS = { record_type: 'Record', occurred_at: 'When', created_at: 'When', happened_at: 'When', item: 'What', participant_id: 'Participant', site_id: 'Site', next_send_at: 'Next send', body_text: 'Message',
  verbatim_text: 'In their words', msg_type: 'Type', template_name: 'Template', delivery_status: 'Delivery', dose_label: 'Dose', n: 'Count',
  'custom:participant_id': 'Participant ID', phone_number: 'Phone', phone_number_verified: 'Phone verified', reminder_id: 'Reminder ID', id: 'ID',
  est_cost: 'Est. cost' };

function table(rows, cols, extra, empty) {
  if (!rows || !rows.length) return empty || emptyState('file', 'Nothing here yet', 'Records appear here as they are created.');
  cols = cols || [...new Set(rows.flatMap(r => Object.keys(r)))].filter(c => !c.startsWith('_'));
  // A {__html} value is markup the caller built (and escaped) itself.
  const label = c => esc(COL_LABELS[c] || humanize(c));
  // Coded values (DOSE, no_response, scheduler) read as words; the stored value stays in the tooltip.
  const words = (c, v) => ENUM_COLS.has(c) && typeof v === 'string' && v ? { __html: `<span title="${esc(v)}">${esc(humanize(v))}</span>` } : v;
  const cell = (c, v) => v && typeof v === 'object' && '__html' in v ? `<td data-label="${label(c)}">${v.__html}</td>`
    : STATUS_WORDS.has(v) ? `<td data-label="${label(c)}">${chip(v)}</td>`
    : `<td data-label="${label(c)}" class="${typeof v === 'object' && v ? 'json' : typeof v === 'string' && v.length > 60 ? 'wrap' : numeric.has(c) ? 'r' : ''}">${fmt(v)}</td>`;
  const NUM = /^-?[\d,.]+(\s?(ms|%))?$/;
  const numeric = new Set(cols.filter(c => rows.every(r => r[c] === null || r[c] === undefined || r[c] === '' || NUM.test(String(r[c]))) && rows.some(r => NUM.test(String(r[c] ?? '')))));
  const align = c => numeric.has(c) ? ' class="r"' : '';
  return `<div class="table-wrap"><table><thead><tr>${cols.map(c => `<th scope="col"${align(c)}>${esc(COL_LABELS[c] || humanize(c))}</th>`).join('')}${extra ? '<th scope="col"><span class="sr-only">Actions</span></th>' : ''}</tr></thead><tbody>`
    + rows.map((r, i) => `<tr>${cols.map(c => cell(c, STATUS_WORDS.has(r[c]) ? r[c] : words(c, r[c]))).join('')}${extra ? `<td class="actions-cell">${extra(r, i)}</td>` : ''}</tr>`).join('')
    + '</tbody></table></div>';
}

// For polled widgets: skip the DOM write when nothing changed, and keep keyboard focus on the same control
// when something did, so a 4 s refresh never yanks focus away from someone tabbing through alerts.
function renderStable(el, html) {
  if (el._html === html) return;
  const controls = () => [...el.querySelectorAll('a, button, input, select, textarea')];
  const at = el.contains(document.activeElement) ? controls().indexOf(document.activeElement) : -1;
  el.innerHTML = html; el._html = html;
  if (at >= 0) controls()[Math.min(at, controls().length - 1)]?.focus({ preventScroll: true });
}

function emptyState(ic, title, text, action = '') {
  return `<div class="empty">${icon(ic)}<b>${esc(title)}</b>${text ? `<p>${esc(text)}</p>` : ''}${action}</div>`;
}
const skeleton = (n = 3) => `<div class="skeleton" aria-hidden="true">${'<i></i>'.repeat(n)}</div><span class="sr-only">Loading</span>`;
const initials = name => String(name || '?').split(/[\s@.]+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join('');

function describeCron(cron) {
  const m = /^cron\((\d+) (\d+) \S+ \S+ (\S+) \S+\)$/.exec(cron || '');
  if (!m) return cron || '';
  const h = +m[2], t = `${(h % 12) || 12}:${String(m[1]).padStart(2, '0')} ${h < 12 ? 'AM' : 'PM'}`;
  const days = m[3] === '*' || m[3] === '?' ? 'Every day' : m[3].split(',').map(d => d.charAt(0) + d.slice(1).toLowerCase()).join(', ');
  return `${days} at ${t}`;
}

function formData(form) {
  const out = {};
  for (const el of form.elements) {
    if (!el.name || el.closest('.hidden')) continue;
    if (el.type === 'checkbox') out[el.name] = el.checked;
    else if (el.value !== '') out[el.name] = el.value;
  }
  return out;
}

// ------------------------------------------------------------ sign-in and sign-out
// One sign-in for both portals: an email address or phone number, and a password, open whichever portal the
// account belongs to.
// The session lives in localStorage as tt.session = { portal, user, name?, groups?, site_ids?, clientId?, id?, refresh?, exp? }.
const Auth = {
  session() { try { return JSON.parse(store('session') || 'null'); } catch { return null; } },
  save(s) { store('session', s ? JSON.stringify(s) : ''); },

  // Resolves to { portal } when signed in, or { challenge, ... } when Cognito needs one more step (pass it to respond).
  // Rejects with an Error whose .code is bad_credentials, mfa_setup, reset_required, too_many or network.
  async signIn(login, password) {
    if (LOCAL) {
      const res = await fetch('/dev/sign-in', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ login, password }) }).catch(() => null);
      if (!res) throw authError('network');
      if (!res.ok) throw authError('bad_credentials');
      const who = await res.json();
      this.save({ portal: who.portal, user: who.username });
      return { portal: who.portal };
    }
    const username = signInName(login);
    for (const portal of Object.keys(PORTALS)) {
      const clientId = CONFIG.clients[portal]; if (!clientId) continue;
      try {
        const r = await cognito('InitiateAuth', { ClientId: clientId, AuthFlow: 'USER_PASSWORD_AUTH',
          AuthParameters: { USERNAME: username, PASSWORD: password } });
        return this.finish(r, { portal, clientId, username });
      } catch (e) { if (e.code !== 'bad_credentials') throw e; }   // not this pool: try the next one
    }
    throw authError('bad_credentials');
  },

  // The answer to a challenge: a new password (NEW_PASSWORD_REQUIRED) or a code (SOFTWARE_TOKEN_MFA, SMS_MFA).
  async respond(step, answer) {
    const key = { NEW_PASSWORD_REQUIRED: 'NEW_PASSWORD', SOFTWARE_TOKEN_MFA: 'SOFTWARE_TOKEN_MFA_CODE', SMS_MFA: 'SMS_MFA_CODE' }[step.challenge];
    const r = await cognito('RespondToAuthChallenge', { ClientId: step.clientId, ChallengeName: step.challenge, Session: step.session,
      ChallengeResponses: { USERNAME: step.username, [key]: answer } });
    return this.finish(r, step);
  },

  finish(r, ctx) {
    if (r.ChallengeName === 'MFA_SETUP') throw authError('mfa_setup');
    if (r.ChallengeName) return { ...ctx, challenge: r.ChallengeName, session: r.Session,
      username: r.ChallengeParameters?.USER_ID_FOR_SRP || ctx.username };
    this.keep(ctx, r.AuthenticationResult);
    return { portal: ctx.portal };
  },

  keep(ctx, result, refresh = result.RefreshToken) {
    const c = jwtClaims(result.IdToken);
    this.save({ portal: ctx.portal, clientId: ctx.clientId, id: result.IdToken, refresh, exp: c.exp,
      user: c.email || c['custom:participant_id'] || c['cognito:username'], name: c.name, groups: c['cognito:groups'] || [],
      site_ids: c['custom:site_ids'] || '' });
  },

  // Headers that identify the signed-in person to the API, refreshing a hosted token that is about to expire.
  async headers() {
    const s = this.session();
    if (s && !LOCAL && s.exp - Date.now() / 1000 < 60) {
      const r = await cognito('InitiateAuth', { ClientId: s.clientId, AuthFlow: 'REFRESH_TOKEN_AUTH',
        AuthParameters: { REFRESH_TOKEN: s.refresh } }).catch(() => null);
      if (!r) { this.signOut('expired'); throw authError('expired'); }
      this.keep(s, r.AuthenticationResult, s.refresh);
    }
    return this.headersNow();
  },
  // The same, without a refresh: for a request that must go out right now (a send on page leave).
  headersNow() {
    if (LOCAL) return { 'X-Dev-Identity': `${TT.portal}:${encodeURIComponent(TT.user || '')}` };
    const s = this.session();
    return s?.id ? { Authorization: `Bearer ${s.id}` } : {};
  },

  // Forgets the session (and, hosted, revokes the refresh token) and goes back to the sign-in page.
  async signOut(reason = 'signed-out') {
    const s = this.session();
    this.save(null);
    if (s?.refresh && !LOCAL) await cognito('RevokeToken', { Token: s.refresh, ClientId: s.clientId }).catch(() => {});
    location.href = `/?${reason}`;
  },
};

const authError = code => Object.assign(new Error(code), { code });
// What the person typed, as a Cognito sign-in name: an email address in lower case, or a phone number as +<digits>
// (ten digits are read as a US number).
function signInName(login) {
  const s = String(login || '').trim();
  if (s.includes('@')) return s.toLowerCase();
  const digits = s.replace(/\D/g, '');
  return '+' + (digits.length === 10 ? '1' + digits : digits);
}
function jwtClaims(token) {
  try { return JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'))); } catch { return {}; }
}
// Cognito's public sign-in API, called straight from the browser (app clients without a secret need no AWS keys).
async function cognito(action, payload) {
  const res = await fetch(`https://cognito-idp.${CONFIG.cognitoRegion}.amazonaws.com/`, { method: 'POST',
    headers: { 'Content-Type': 'application/x-amz-json-1.1', 'X-Amz-Target': `AWSCognitoIdentityProviderService.${action}` },
    body: JSON.stringify(payload) }).catch(() => null);
  if (!res) throw authError('network');
  const data = await res.json().catch(() => ({}));
  if (res.ok) return data;
  const type = String(data.__type || '').split('#').pop();
  throw authError({ NotAuthorizedException: 'bad_credentials', UserNotFoundException: 'bad_credentials',
    PasswordResetRequiredException: 'reset_required', TooManyRequestsException: 'too_many', LimitExceededException: 'too_many',
    TooManyFailedAttemptsException: 'too_many', CodeMismatchException: 'bad_code', ExpiredCodeException: 'bad_code',
    InvalidPasswordException: 'weak_password' }[type] || 'failed');
}

// ------------------------------------------------------------ local accounts (dev server only)
async function reloadIdentities() {
  const ids = await dev('GET', '/dev/identities');
  const list = ids[TT.portal] || [];
  const s = Auth.session(), keep = TT.user || (s?.portal === TT.portal ? s.user : null);
  TT.identities = list;
  TT.user = list.some(u => u.username === keep && u.enabled !== false) ? keep : null;
  TT.identity = list.find(u => u.username === TT.user) || null;
  renderAccount();
}

// ------------------------------------------------------------ worst-case test data (dev only)
// Realistic worst values, entered through the real API exactly as demo data is: long and accented names, a
// one-letter name, unbounded emails, multi-paragraph messages, a chat-detected emergency, counts of exactly 1.
async function seedWorstCase() {
  const ids = await dev('GET', '/dev/identities');
  const lead = ids.staff.find(u => /lead@/.test(u.username) || /lead@/.test(u.email || ''))?.username || ids.staff[0].username;
  const call = async (who, method, path, body) => {
    const res = await fetch('/api' + path, { method, headers: { 'X-Dev-Identity': who, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(`${method} ${path}: ${data.error || res.status}`);
    return data;
  };
  const staff = 'staff:' + encodeURIComponent(lead);
  const people = [
    { full_name: 'Aleksandra Wiśniewska-Kowalczyk', phone_e164: '+17325550123', email: 'aleksandra.wisniewska-kowalczyk@northwind-industries-holdings.example.org',
      site_id: 'RIVERSIDE', preferred_language: 'es', timezone: 'America/New_York', status: 'enrolled', whatsapp_opt_in: true },
    { full_name: 'Jo', phone_e164: '+17325550124', site_id: 'RIVERSIDE', timezone: 'America/New_York', status: 'screening', whatsapp_opt_in: false },
    { full_name: 'Nguyễn Thị Phương Thảo', phone_e164: '+13125550125', email: 'thao.nguyen@example.org', site_id: 'LAKEVIEW',
      timezone: 'America/Chicago', status: 'enrolled', whatsapp_opt_in: true },
    { full_name: 'Maximilian Alexander von Hohenzollern-Sigmaringen III', phone_e164: '+13125550126', site_id: 'LAKEVIEW',
      timezone: 'America/Chicago', status: 'screen_failed', whatsapp_opt_in: false },
  ];
  const pids = [];
  for (const p of people) pids.push((await call(staff, 'POST', '/staff/participants', { study_id: 'KOA-301', ...p })).participant.participant_id);
  const [ola, jo, thao] = pids.map(id => 'participant:' + encodeURIComponent(id));
  await call(staff, 'POST', `/staff/participants/${pids[0]}/reminders`, { reminder_type: 'dose', label: 'Evening dose after dinner, with food and a full glass of water',
    dose_label: 'evening (2 tablets)', dose_instructions: 'Take 2 tablets with food. Do not take with grapefruit juice. If you miss a dose, skip it.',
    time: '20:30', days: ['MON', 'WED', 'FRI', 'SUN'], escalate_after_misses: 3 });
  await call(ola, 'POST', '/me/emergency', { message: 'Since this afternoon my left knee is very swollen and hot, and now I feel dizzy when I stand up. '
    + 'I took the evening dose at 8:30 like always. My daughter is with me but she has to leave at 10. Please call my mobile, not the house phone.' });
  await call(thao, 'POST', '/me/chat', { text: "I have chest pain and can't breathe properly" });
  await call(ola, 'POST', '/me/requests', { kind: 'reminder_change', message: 'Hello, could the evening reminder move to 21:15 on weekdays?\n\n'
    + 'On Mondays and Wednesdays I work until 20:45 and I cannot take the tablets with food before then. On weekends 20:30 is still fine. '
    + 'Also, the reminder arrives in English but I chose Spanish. Thank you!' });
  await call(jo, 'POST', '/me/requests', { kind: 'contact_team', message: 'ok' });
  await call(thao, 'POST', '/me/requests', { kind: 'withdrawal', message: 'I am moving to Hanoi next month. Can I still be in the study?' });
  await call(ola, 'POST', '/me/requests', { kind: 'whatsapp_opt_out', message: 'Please stop the WhatsApp messages, my phone is shared with my son.' });
  await call(staff, 'POST', '/staff/accounts', { email: 'bartholomew.fitzgerald-montgomery@northwind-industries-holdings.example.com',
    name: 'Dr. Bartholomew Fitzgerald-Montgomery', role: 'PI', site_ids: ['RIVERSIDE', 'LAKEVIEW'], study_ids: ['KOA-301'] });
}

async function switchDataset(set) {
  const ok = await ask({ title: set === 'worst' ? 'Load worst-case data?' : 'Go back to demo data?',
    message: 'This deletes all local data in every portal and reloads the seed' + (set === 'worst' ? ', plus realistic worst-case records.' : '.'),
    confirm: set === 'worst' ? 'Load worst case' : 'Load demo data' });
  if (!ok) return;
  await dev('POST', '/dev/reset');
  try { if (set === 'worst') await seedWorstCase(); } catch (e) { toast('Unable to load the worst case: ' + e.message, true); }
  location.reload();
}

// Call once per page: renders the shared header, loads the accounts, and calls onSignIn(user|null) on every change.
// Signed out, or signed in to the other portal: back to the sign-in page, which returns here afterwards.
async function initPortal(portal, onSignIn) {
  TT.portal = portal; TT.onSignIn = onSignIn;
  const s = Auth.session();
  const toSignIn = () => location.replace(`/?next=${encodeURIComponent(location.pathname + location.hash)}`);
  if (s?.portal !== portal) return toSignIn();
  injectAssets();
  renderChrome(portal);
  if (LOCAL) {
    await reloadIdentities();
    if (!TT.user) { Auth.save(null); return toSignIn(); }   // the account is gone (data reset) or disabled
  } else {
    TT.user = s.user; TT.identities = [];
    TT.identity = { username: s.user, name: s.name, groups: s.groups, site_ids: s.site_ids };
    renderAccount();
  }
  signedIn();
}

function signedIn() {
  $$('[data-signed-in]').forEach(el => el.classList.toggle('hidden', !TT.user));
  $$('[data-signed-out]').forEach(el => el.classList.toggle('hidden', !!TT.user));
  Promise.resolve(TT.onSignIn(TT.user)).catch(e => console.error(e));
}
