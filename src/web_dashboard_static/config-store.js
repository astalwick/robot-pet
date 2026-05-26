const SAVE_DEBOUNCE_MS = 350;

async function postConfig(name, body) {
  const response = await fetch(`/config/${name}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch (err) {
    // Non-JSON body — fall through to generic error.
  }
  if (!response.ok) {
    return { ok: false, error: payload.error || `config ${name} apply failed (${response.status})` };
  }
  return { ok: true, payload };
}

function valuesEqual(a, b) {
  if (typeof a === 'boolean' || typeof b === 'boolean') return a === b;
  if (typeof a === 'string' || typeof b === 'string') return a === b;
  return Number(a) === Number(b);
}

function createSection(name) {
  const section = {
    name,
    server: {},
    fields: [],
    local: {},
    saveWork: Promise.resolve(),
    debounceTimer: null,

    get(key) {
      if (key in section.local) return section.local[key];
      return section.server[key];
    },

    hasLocal(key) {
      return key in section.local;
    },

    ingestServer(partial) {
      if (!partial) return;
      for (const key of Object.keys(partial)) {
        const incoming = partial[key];
        if (key in section.local) {
          if (valuesEqual(incoming, section.local[key])) {
            section.server[key] = incoming;
            delete section.local[key];
          }
          continue;
        }
        section.server[key] = incoming;
      }
    },

    async load() {
      const response = await fetch(`/config/${name}`);
      const payload = await response.json();
      section.server = payload.values || {};
      section.fields = payload.fields || [];
      section.local = {};
      return { error: payload.error, ok: response.ok };
    },

    set(partial) {
      Object.assign(section.local, partial);
      clearTimeout(section.debounceTimer);
      section.debounceTimer = setTimeout(() => {
        section.debounceTimer = null;
        section.queueSave();
      }, SAVE_DEBOUNCE_MS);
    },

    flush() {
      clearTimeout(section.debounceTimer);
      section.debounceTimer = null;
      return section.queueSave();
    },

    async apply(values) {
      const body = { ...section.server, ...values };
      const result = await postConfig(name, body);
      if (result.ok) {
        section.server = { ...section.server, ...values };
        section.local = {};
        if (result.payload && result.payload.values) {
          section.server = result.payload.values;
        }
      }
      return result;
    },

    queueSave() {
      section.saveWork = section.saveWork.then(() => section.runSave());
      return section.saveWork;
    },

    async runSave() {
      while (true) {
        const dirtyKeys = Object.keys(section.local).filter(
          (key) => !valuesEqual(section.local[key], section.server[key]),
        );
        if (dirtyKeys.length === 0) break;

        const submitted = {};
        for (const key of dirtyKeys) submitted[key] = section.local[key];

        const body = { ...section.server, ...section.local };
        const result = await postConfig(name, body);
        if (!result.ok) {
          for (const key of Object.keys(submitted)) {
            if (key in section.local && valuesEqual(section.local[key], submitted[key])) {
              delete section.local[key];
            }
          }
          return result;
        }
        if (result.payload && result.payload.values) {
          section.server = result.payload.values;
        } else {
          for (const key of Object.keys(submitted)) {
            if (key in section.local && valuesEqual(section.local[key], submitted[key])) {
              section.server[key] = section.local[key];
            }
          }
        }
      }
      return { ok: true };
    },
  };
  return section;
}

export const configStore = {
  drive: createSection('drive'),
  vision: createSection('vision'),
  voice: createSection('voice'),
  sensors: createSection('sensors'),
};

export async function loadAll() {
  const results = await Promise.all([
    configStore.drive.load(),
    configStore.vision.load(),
    configStore.voice.load(),
    configStore.sensors.load(),
  ]);
  return results;
}
