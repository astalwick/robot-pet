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

function createSection(name) {
  const section = {
    name,
    server: {},
    fields: [],
    local: {},
    saveWork: Promise.resolve(),

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
        if (!(key in section.local)) {
          section.server[key] = partial[key];
        }
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

    patch(partial) {
      Object.assign(section.local, partial);
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
      while (Object.keys(section.local).length > 0) {
        const body = { ...section.server, ...section.local };
        const result = await postConfig(name, body);
        if (!result.ok) {
          return result;
        }
        const savedKeys = Object.keys(section.local);
        if (result.payload && result.payload.values) {
          section.server = result.payload.values;
        } else {
          for (const key of savedKeys) {
            section.server[key] = section.local[key];
          }
        }
        for (const key of savedKeys) {
          delete section.local[key];
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
};

export async function loadAll() {
  const results = await Promise.all([
    configStore.drive.load(),
    configStore.vision.load(),
    configStore.voice.load(),
  ]);
  return results;
}
