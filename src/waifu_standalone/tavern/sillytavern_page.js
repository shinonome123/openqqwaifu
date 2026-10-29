// Runs inside SillyTavern's own page (opened by a headless browser) as ``window.openqqwaifu``.
// The tavern calls these through the DevTools protocol (sillytavern.py); everything a game
// needs from SillyTavern goes through its own functions, so a card is played exactly as
// SillyTavern plays it: its prompt order, lorebook, regex scripts, preset and API settings.
(() => {
    const VERSION = 4;
    if (window.openqqwaifu && window.openqqwaifu.version === VERSION) {
        return;
    }
    // Where SillyTavern's setExtensionPrompt puts our note: in the chat at depth 0, as a system message.
    const IN_CHAT = 1;
    const SYSTEM = 0;
    const NOTE_KEY = 'openqqwaifu';
    let modules = null;

    async function st() {
        if (!modules) {
            const [s, oai, wi, ext, regex, events, pu] = await Promise.all([
                import('/script.js'),
                import('/scripts/openai.js'),
                import('/scripts/world-info.js'),
                import('/scripts/extensions.js'),
                import('/scripts/extensions/regex/engine.js'),
                import('/scripts/events.js'),
                import('/scripts/power-user.js'),
            ]);
            modules = { s, oai, wi, ext, regex, events, pu };
        }
        return modules;
    }

    // Nobody is there to answer SillyTavern's popups ("import the lorebook?"): each is cancelled.
    // The welcome page of a first start is saved as it is, since SillyTavern waits on it to start.
    // 酒馆助手 (JS-Slash-Runner) asks once per card or preset whether to run the scripts it carries:
    // yes, as for a card's regex scripts.
    function closePopups() {
        document.querySelectorAll('.TH-popup[role="dialog"]').forEach((popup) => {
            if (/嵌入式脚本/.test(popup.textContent || '')) {
                popup.querySelector('.popup-button-ok')?.click();
            }
        });
        document.querySelectorAll('dialog[open]').forEach((dialog) => {
            const answer = dialog.querySelector('.onboarding') ? '.popup-button-ok' : '.popup-button-cancel, .popup-button-close';
            const button = dialog.querySelector(answer);
            try {
                if (button) {
                    button.click();
                } else {
                    dialog.close();
                }
            } catch {
                // already closing
            }
        });
    }

    function waitFor(check, timeoutMs) {
        return new Promise((resolve, reject) => {
            const started = Date.now();
            const tick = () => {
                if (check()) return resolve(true);
                if (Date.now() - started > timeoutMs) return reject(new Error('timed out'));
                setTimeout(tick, 100);
            };
            tick();
        });
    }

    function sleep(ms) {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }

    function errorToasts() {
        return [...document.querySelectorAll('#toast-container .toast-error')].map((toast) => toast.innerText.trim()).join(' ').slice(0, 300);
    }

    async function ready() {
        const { events } = await st();
        // APP_READY fires again for a listener that comes after it.
        let started = false;
        events.eventSource.on(events.event_types.APP_READY, () => { started = true; });
        while (!started) {
            closePopups();
            await new Promise((resolve) => setTimeout(resolve, 250));
        }
        closePopups();
        return true;
    }

    // Chat completion from an OpenAI-compatible endpoint, set the way SillyTavern's own form sets it.
    async function connect(options) {
        const { s, oai, pu } = await st();
        closePopups();
        if (s.main_api !== 'openai') {
            $('#main_api').val('openai').trigger('change');
        }
        if (oai.oai_settings.chat_completion_source !== 'custom') {
            $('#chat_completion_source').val('custom').trigger('change');
        }
        $('#custom_api_url_text').val(options.url).trigger('input');
        $('#custom_model_id').val(options.model).trigger('input');
        if (options.reasoning) {
            $('#openai_reasoning_effort').val(options.reasoning).trigger('change');
        }
        if (options.firstTime) {
            // SillyTavern starts with a 4k context and 300-token replies; cards need more.
            oai.oai_settings.openai_max_context = options.contextTokens;
            oai.oai_settings.openai_max_tokens = options.replyTokens;
            $('#openai_max_context').val(options.contextTokens);
            $('#openai_max_context_counter').val(options.contextTokens);
            $('#openai_max_tokens').val(options.replyTokens);
            // The tavern imports a card's lorebook itself; the question would only pile up.
            pu.power_user.world_import_dialog = false;
        }
        if (options.key) {
            $('#api_key_custom').val(options.key);
        }
        // The connect button saves the key and checks the endpoint.
        $('#api_button_openai').trigger('click');
        await waitFor(() => s.online_status !== 'no_connection', 20000).catch(() => null);
        // Now, not debounced: the page may be loaded again before a debounced save comes.
        await s.saveSettings();
        return { online: s.online_status };
    }

    async function connection() {
        const { s, oai } = await st();
        return { api: s.main_api, source: oai.oai_settings.chat_completion_source, url: oai.oai_settings.custom_url, model: oai.oai_settings.custom_model };
    }

    // A reasoning model's <think> block becomes the message's reasoning, out of its text and the
    // history sent back, as SillyTavern's "Auto-Parse" does.
    async function parseReasoning() {
        const { s, pu } = await st();
        pu.power_user.reasoning.auto_parse = true;
        $('#reasoning_auto_parse').prop('checked', true);
        await s.saveSettings();
        return true;
    }

    async function online() {
        const { s } = await st();
        if (s.online_status === 'no_connection' && s.main_api === 'openai') {
            $('#api_button_openai').trigger('click');
            await waitFor(() => s.online_status !== 'no_connection', 20000).catch(() => null);
        }
        return s.online_status;
    }

    async function extensions() {
        const { s } = await st();
        const response = await fetch('/api/extensions/discover', { headers: s.getRequestHeaders() });
        const listed = response.ok ? await response.json() : [];
        return listed.map((item) => item.name);
    }

    // Installed the way SillyTavern's "Install extension" does: a git clone for this user.
    async function installExtension(options) {
        const { s } = await st();
        const response = await fetch('/api/extensions/install', {
            method: 'POST',
            headers: s.getRequestHeaders(),
            body: JSON.stringify({ url: options.url, branch: options.branch || undefined, global: false }),
        });
        if (!response.ok) {
            throw new Error(`SillyTavern did not install ${options.url} (${response.status}: ${(await response.text()).slice(0, 200)})`);
        }
        return true;
    }

    async function deleteExtension(options) {
        const { s } = await st();
        const response = await fetch('/api/extensions/delete', {
            method: 'POST',
            headers: s.getRequestHeaders(),
            body: JSON.stringify({ extensionName: options.name, global: false }),
        });
        if (!response.ok) {
            throw new Error(`SillyTavern did not remove ${options.name} (${response.status})`);
        }
        return true;
    }

    // 酒馆助手 raises the context to 2M tokens by default ("最大化预设上下文长度"), so SillyTavern
    // would never cut a long game down to the model's window. That one is turned off in its own
    // settings store (it keeps a copy of what it saved, and raises the context again on every save).
    async function keepContext(options) {
        const { s, oai, ext } = await st();
        const pinia = document.getElementById('tavern_helper')?.__vue_app__?.config?.globalProperties?.$pinia;
        const store = pinia?._s?.get('global_settings');
        let helper = 'none';
        if (store?.settings?.optimize) {
            store.settings.optimize.maximize_preset_context_length = false;
            helper = 'store';
            await sleep(200); // its watcher writes the change back to SillyTavern's settings
        } else if (ext.extension_settings.tavern_helper) {
            ext.extension_settings.tavern_helper.optimize = {
                ...(ext.extension_settings.tavern_helper.optimize || {}), maximize_preset_context_length: false,
            };
            helper = 'settings';
        }
        oai.oai_settings.openai_max_context = options.contextTokens;
        $('#openai_max_context').val(options.contextTokens);
        $('#openai_max_context_counter').val(options.contextTokens);
        await s.saveSettings();
        await sleep(200);
        return { context: oai.oai_settings.openai_max_context, helper };
    }

    async function characters() {
        const { s } = await st();
        await s.getCharacters();
        return s.characters.map((c) => {
            const data = c.data || {};
            const book = data.character_book && Array.isArray(data.character_book.entries) ? data.character_book.entries : [];
            return {
                avatar: c.avatar,
                name: c.name,
                description: data.description ?? c.description ?? '',
                personality: data.personality ?? c.personality ?? '',
                scenario: data.scenario ?? c.scenario ?? '',
                first_mes: data.first_mes ?? c.first_mes ?? '',
                alternate_greetings: Array.isArray(data.alternate_greetings) ? data.alternate_greetings : [],
                system_prompt: data.system_prompt ?? '',
                post_history_instructions: data.post_history_instructions ?? '',
                mes_example: data.mes_example ?? c.mes_example ?? '',
                creator_notes: data.creator_notes ?? c.creatorcomment ?? '',
                tags: data.tags ?? c.tags ?? [],
                extensions: data.extensions ?? {},
                lore: book.map((entry) => String(entry.content ?? '')).join('\n'),
            };
        });
    }

    async function open(avatar, chat) {
        const { s } = await st();
        closePopups();
        let id = s.characters.findIndex((c) => c.avatar === avatar);
        if (id < 0) {
            await s.getCharacters();
            id = s.characters.findIndex((c) => c.avatar === avatar);
        }
        if (id < 0) {
            throw new Error(`no character ${avatar}`);
        }
        if (String(s.this_chid) !== String(id)) {
            await s.selectCharacterById(String(id));
        }
        if (chat && s.getCurrentChatId() !== chat) {
            await s.openCharacterChat(chat);
        }
        if (String(s.this_chid) !== String(id) || (chat && s.getCurrentChatId() !== chat)) {
            throw new Error(`could not open ${avatar} / ${chat}`);
        }
        if (helperScripts(s.characters[id])) {
            await enableHelperScripts(avatar);
        }
        return id;
    }

    // The scripts a card carries for 酒馆助手, when that extension is there to run them.
    function helperScripts(character) {
        if (!window.TavernHelper) {
            return false;
        }
        let helper = character?.data?.extensions?.tavern_helper;
        if (Array.isArray(helper)) {
            helper = Object.fromEntries(helper); // its older format
        }
        return Array.isArray(helper?.scripts) && helper.scripts.length > 0;
    }

    // 酒馆助手 asks whether to run them once the card is open; wait for that and say yes.
    async function enableHelperScripts(avatar) {
        const { ext } = await st();
        const deadline = Date.now() + 3000;
        while (Date.now() < deadline) {
            const script = ext.extension_settings.tavern_helper?.script;
            if (script?.enabled?.characters?.includes(avatar)) {
                return;
            }
            const asking = document.querySelector('.TH-popup[role="dialog"]');
            if (!asking && script?.popuped?.characters?.includes(avatar)) {
                return; // asked before, and turned down in SillyTavern's own page
            }
            closePopups();
            await sleep(200);
        }
    }

    // A card's scripts may still be rewriting the reply (酒馆助手's MESSAGE_RECEIVED handlers).
    // In a new chat they are still loading, so the first change may take up to ``patienceMs``.
    async function settled(s, patienceMs = 0) {
        const latest = () => s.chat[s.chat.length - 1]?.mes;
        let text = latest();
        const started = Date.now();
        while (Date.now() - started < patienceMs && latest() === text) {
            await sleep(200);
        }
        text = latest();
        for (let i = 0; i < 10; i++) {
            await sleep(300);
            const now = latest();
            if (now === text) {
                return;
            }
            text = now;
        }
    }

    // What SillyTavern's "Yes" to "import the embedded lorebook?" does, without the editor.
    async function linkLore(id, force) {
        const { s, wi, ext } = await st();
        const c = s.characters[id];
        const book = c?.data?.character_book;
        const linked = c?.data?.extensions?.world;
        if (!book || (!force && linked && wi.world_names.includes(linked))) {
            return;
        }
        let name = linked || book.name || `${c.name}'s Lorebook`;
        if (!linked && wi.world_names.includes(name)) {
            name = `${c.name}'s Lorebook`;
        }
        await wi.saveWorldInfo(name, wi.convertCharacterBook(book), true);
        await wi.updateWorldInfoList();
        await ext.writeExtensionField(id, 'world', name);
    }

    // A card's own regex scripts run, as when its importer says yes to them.
    async function allowScripts(id) {
        const { s, regex } = await st();
        const c = s.characters[id];
        if (Array.isArray(c?.data?.extensions?.regex_scripts) && c.data.extensions.regex_scripts.length) {
            regex.allowScopedScripts(c);
        }
    }

    async function importCard(options) {
        const { s } = await st();
        closePopups();
        const bytes = Uint8Array.from(atob(options.data), (ch) => ch.charCodeAt(0));
        const form = new FormData();
        const type = options.type === 'png' ? 'image/png' : 'application/json';
        form.append('avatar', new Blob([bytes], { type }), `card.${options.type}`);
        form.append('file_type', options.type);
        if (options.replace) {
            form.append('preserved_name', options.replace);
        }
        const response = await fetch('/api/characters/import', {
            method: 'POST',
            headers: s.getRequestHeaders({ omitContentType: true }),
            body: form,
            cache: 'no-cache',
        });
        const result = response.ok ? await response.json() : { error: true };
        if (result.error || !result.file_name) {
            throw new Error(`SillyTavern did not take the card (${response.status})`);
        }
        await s.getCharacters();
        const avatar = `${result.file_name}.png`;
        const id = s.characters.findIndex((c) => c.avatar === avatar);
        if (id < 0) {
            throw new Error(`the imported card ${avatar} is not listed`);
        }
        await linkLore(id, Boolean(options.replace));
        await allowScripts(id);
        return { avatar, name: s.characters[id].name };
    }

    async function start(options) {
        const { s } = await st();
        const id = await open(options.avatar);
        await linkLore(id, false);
        await allowScripts(id);
        // The greeting's {{user}} is the players. The chat keeps SillyTavern's own name: renaming it
        // would load it again, and a card's 酒馆助手 scripts would take its greeting a second time.
        s.setUserName(options.user, { toastPersonaNameChange: false });
        await s.doNewChat();
        const chat = s.getCurrentChatId();
        const greeting = s.chat.find((m) => !m.is_user && !m.is_system);
        // A card that opens with a credits page tells its story in another greeting: swipe to it.
        const index = Number(options.greeting) || 0;
        if (greeting && index > 0 && Array.isArray(greeting.swipes) && greeting.swipes[index] !== undefined) {
            greeting.swipe_id = index;
            greeting.mes = greeting.swipes[index];
            s.updateMessageBlock(s.chat.indexOf(greeting), greeting);
            await s.saveChatConditional();
            const { events } = await st();
            await events.eventSource.emit(events.event_types.MESSAGE_SWIPED, s.chat.indexOf(greeting));
        }
        if (helperScripts(s.characters[id])) {
            await settled(s, 3000);
        }
        return { chat, greeting: greeting ? greeting.mes : '' };
    }

    function lastReply(chat) {
        const message = [...chat].reverse().find((m) => !m.is_user && !m.is_system);
        return message ? message.mes : '';
    }

    async function resume(options) {
        const { s } = await st();
        await open(options.avatar, options.chat);
        return { last: lastReply(s.chat) };
    }

    async function prepare(options) {
        const { s } = await st();
        await open(options.avatar, options.chat);
        s.setUserName(options.user, { toastPersonaNameChange: false });
        SillyTavern.getContext().setExtensionPrompt(NOTE_KEY, options.note || '', IN_CHAT, 0, false, SYSTEM);
        document.querySelectorAll('#toast-container .toast-error').forEach((toast) => toast.remove());
        return s;
    }

    // The players' round as the user's message, then SillyTavern writes the character's reply.
    async function turn(options) {
        const s = await prepare(options);
        if ((await online()) === 'no_connection') {
            throw new Error('SillyTavern is not connected to a model');
        }
        const before = s.chat.length;
        await s.sendMessageAsUser(options.text);
        let failure = null;
        try {
            await s.Generate('normal');
        } catch (error) {
            failure = error; // the endpoint answered with an error
        }
        if (!failure && helperScripts(s.characters[s.this_chid])) {
            await settled(s);
        }
        const last = s.chat[s.chat.length - 1];
        if (failure || !last || last.is_user || s.chat.length <= before + 1) {
            const error = failure ? String(failure.message || failure) : errorToasts();
            // The round is sent again with the next one; it must not stay here twice.
            if (s.chat.length > before && s.chat[s.chat.length - 1].is_user) {
                await s.deleteLastMessage();
                await s.saveChatConditional();
            }
            throw new Error(`no reply${error ? `: ${error}` : ''}`);
        }
        return { reply: last.mes };
    }

    // A reply written again from the same chat (the note says how), not added to it.
    async function quiet(options) {
        const s = await prepare(options);
        const text = await s.generateQuietPrompt({ quietPrompt: options.instruction });
        return { text: String(text || '') };
    }

    async function replaceLast(options) {
        const { s } = await st();
        await open(options.avatar, options.chat);
        const last = s.chat[s.chat.length - 1];
        if (!last || last.is_user) {
            throw new Error('the last message is not a reply');
        }
        last.mes = options.text;
        if (Array.isArray(last.swipes)) {
            last.swipes[last.swipe_id ?? 0] = options.text;
        }
        s.updateMessageBlock(s.chat.length - 1, last);
        await s.saveChatConditional();
        return true;
    }

    function stop() {
        if (modules) {
            modules.s.stopGeneration();
        }
        return true;
    }

    async function chats(options) {
        const { s } = await st();
        const response = await fetch('/api/characters/chats', {
            method: 'POST',
            headers: s.getRequestHeaders(),
            body: JSON.stringify({ avatar_url: options.avatar, simple: true }),
        });
        const result = response.ok ? await response.json() : [];
        return Array.isArray(result) ? result.map((item) => item.file_id) : [];
    }

    // An old game's story as a chat of this character, for "继续酒馆".
    async function saveChat(options) {
        const { s } = await st();
        const c = s.characters.find((item) => item.avatar === options.avatar);
        if (!c) {
            throw new Error(`no character ${options.avatar}`);
        }
        const date = new Date(options.created * 1000).toISOString();
        const header = { user_name: options.user, character_name: c.name, create_date: date, chat_metadata: {} };
        const messages = options.messages.map((message) => ({
            name: message.user ? options.user : c.name,
            is_user: message.user,
            is_system: false,
            send_date: date,
            mes: message.text,
            extra: {},
        }));
        const response = await fetch('/api/chats/save', {
            method: 'POST',
            headers: s.getRequestHeaders(),
            body: JSON.stringify({ ch_name: c.name, file_name: options.chat, chat: [header, ...messages], avatar_url: c.avatar }),
        });
        if (!response.ok) {
            throw new Error(`SillyTavern did not save the chat (${response.status})`);
        }
        return true;
    }

    async function picture(options) {
        const response = await fetch(`/characters/${encodeURIComponent(options.avatar)}`, { cache: 'no-cache' });
        if (!response.ok) {
            return '';
        }
        const bytes = new Uint8Array(await response.arrayBuffer());
        let binary = '';
        for (let i = 0; i < bytes.length; i += 0x8000) {
            binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
        }
        return btoa(binary);
    }

    window.openqqwaifu = {
        version: VERSION, ready, connection, connect, parseReasoning, extensions, installExtension, deleteExtension, keepContext, characters, importCard, start, resume, turn, quiet, replaceLast, stop, chats, saveChat, picture,
    };
})();
