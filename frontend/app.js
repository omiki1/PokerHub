// 网页只负责显示和提交动作。
// 能不能出牌、这一回合谁得分、整局谁赢，都等服务器的 WebSocket 消息。
// 抽牌比点和斗地主共用这一份代码：牌局状态分别放在 game 和 ddz 里。
const SUIT = { S: "♠", H: "♥", D: "♦", C: "♣" };

// 斗地主的点数：3 最小，2 比 A 大，大小王最大。
const DDZ_POINT = { J: 11, Q: 12, K: 13, A: 14, "2": 15, BJ: 16, RJ: 17 };

const { createApp } = Vue;

// 音效用浏览器合成，不另外放音频文件。第一次点击后才能出声。
const Sfx = {
  ctx: null,
  muted: localStorage.getItem("pokerhub_mute") === "1",
  ensure() {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx || this.muted) return null;
    if (!this.ctx) this.ctx = new Ctx();
    if (this.ctx.state === "suspended") this.ctx.resume();
    return this.ctx;
  },
  tone(freq, dur, type, gain, glide) {
    const ctx = this.ctx;
    if (!ctx) return;
    const t = ctx.currentTime;
    const osc = ctx.createOscillator();
    const amp = ctx.createGain();
    osc.type = type || "sine";
    osc.frequency.setValueAtTime(freq, t);
    if (glide) osc.frequency.exponentialRampToValueAtTime(Math.max(40, glide), t + dur);
    amp.gain.setValueAtTime(0.0001, t);
    amp.gain.exponentialRampToValueAtTime(gain, t + 0.012);
    amp.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    osc.connect(amp);
    amp.connect(ctx.destination);
    osc.start(t);
    osc.stop(t + dur + 0.02);
  },
  noise(dur, gain) {
    const ctx = this.ctx;
    if (!ctx) return;
    const length = Math.max(1, Math.floor(ctx.sampleRate * dur));
    const buffer = ctx.createBuffer(1, length, ctx.sampleRate);
    const data = buffer.getChannelData(0);
    for (let i = 0; i < length; i++) data[i] = (Math.random() * 2 - 1) * (1 - i / length);
    const src = ctx.createBufferSource();
    src.buffer = buffer;
    const filter = ctx.createBiquadFilter();
    filter.type = "highpass";
    filter.frequency.value = 700;
    const amp = ctx.createGain();
    amp.gain.value = gain;
    src.connect(filter);
    filter.connect(amp);
    amp.connect(ctx.destination);
    src.start();
  },
  play(name) {
    if (!this.ensure()) return;
    if (name === "card") {
      this.noise(0.07, 0.32);
      this.tone(620, 0.05, "triangle", 0.05);
    } else if (name === "deal") {
      this.noise(0.05, 0.2);
      setTimeout(() => this.play("tick"), 80);
      setTimeout(() => this.play("tick"), 150);
    } else if (name === "tick") {
      if (!this.ensure()) return;
      this.noise(0.04, 0.16);
    } else if (name === "pass") {
      this.tone(170, 0.14, "sine", 0.07, 80);
    } else if (name === "bid") {
      this.tone(523, 0.08, "sine", 0.06);
      setTimeout(() => {
        if (this.ensure()) this.tone(784, 0.12, "sine", 0.05);
      }, 90);
    } else if (name === "reveal") {
      this.tone(146, 0.42, "sawtooth", 0.035, 90);
      this.tone(220, 0.32, "triangle", 0.05);
    } else if (name === "turn") {
      this.tone(880, 0.09, "sine", 0.045);
    } else if (name === "win") {
      [523, 659, 784, 1046].forEach((freq, index) => {
        setTimeout(() => {
          if (this.ensure()) this.tone(freq, 0.16, "triangle", 0.06);
        }, index * 110);
      });
    } else if (name === "lose") {
      [392, 330, 247].forEach((freq, index) => {
        setTimeout(() => {
          if (this.ensure()) this.tone(freq, 0.18, "sine", 0.055);
        }, index * 130);
      });
    } else if (name === "point") {
      this.tone(988, 0.08, "sine", 0.045);
    }
  },
};

function freshLines(prev, next) {
  const before = prev || [];
  const after = next || [];
  if (after.length >= before.length && before.every((line, index) => line === after[index])) {
    return after.slice(before.length);
  }
  return after.length ? [after[after.length - 1]] : [];
}

function soundFor(lines) {
  const text = lines.join("\n");
  if (!text) return "";
  if (text.includes("化为狼人") || text.includes("当地主")) return "reveal";
  if (text.includes("出了") || text.includes("打出")) return "card";
  if (text.includes("不叫") || text.includes("不出")) return "pass";
  if (text.includes("叫")) return "bid";
  if (text.includes("重新发牌") || text.includes("对局开始") || text.includes("发牌结束")) return "deal";
  if (text.includes("抓了")) return "tick";
  if (text.includes("得 1 分") || text.includes("平局")) return "point";
  return "";
}

createApp({
  data() {
    return {
      panel: "login",
      authMode: "login",
      token: "",
      user: null,
      form: { username: "", password: "", nickname: "" },
      message: "",
      messageBad: false,
      rules: { title: "", lines: [] },
      rulesGame: "poker",
      catalog: [],
      selected: [],
      history: [],
      game: null,
      guestPlayId: "",
      waiting: false,
      waitGame: "poker",
      ws: null,
      connecting: null,
      // 斗地主
      ddz: null,
      ddzPlayId: "",
      ddzSelection: [],
      ddzHintOptions: [],
      ddzHintIndex: 0,
      ddzTimer: null,
      rematchWaiting: false,
      muted: Sfx.muted,
    };
  },

  computed: {
    sortedHand() {
      if (!this.game) return [];
      return this.game.you.hand.slice().sort((a, b) => this.point(b) - this.point(a));
    },
    canDraw() {
      const game = this.game;
      if (!game || game.status !== "playing" || !game.you.is_turn) return false;
      return !game.you.drew_this_turn && game.you.deck_left > 0 && game.you.hand.length < game.limits.hand;
    },
    board() {
      const game = this.game;
      if (!game) return { you: null, opponent: null, caption: "" };
      if (game.table.you || game.table.opponent) {
        return { you: game.table.you, opponent: game.table.opponent, caption: "本回合" };
      }
      if (game.last_trick) {
        const words = { you: "你赢得这一回合", opponent: "你输掉这一回合", draw: "这一回合平局" };
        return {
          you: game.last_trick.you,
          opponent: game.last_trick.opponent,
          caption: words[game.last_trick.result],
        };
      }
      return { you: null, opponent: null, caption: "等待出牌" };
    },
    ddzHand() {
      if (!this.ddz) return [];
      // 大牌放右边，和牌桌上的习惯一致。
      return this.ddz.you.hand.slice().sort((a, b) => this.ddzPoint(b) - this.ddzPoint(a));
    },
    ddzMode() {
      return this.ddz ? this.ddz.mode : "";
    },
    ddzCaption() {
      const game = this.ddz;
      if (!game) return "";
      if (game.phase === "finished") return game.result.reason;
      const rival = game.others.find((item) => item.is_turn || item.is_bid_turn);
      const who = game.you.is_turn || game.you.is_bid_turn ? "你" : this.loreName(rival ? rival.name : "");
      if (game.phase === "bidding") {
        return who === "你" ? "轮到你叫分" : `${who} 正在叫分…`;
      }
      return who === "你" ? "轮到你出牌" : `${who} 正在想…`;
    },
    waitText() {
      if (this.waitGame === "ddz") return "正在等第三位旅人…";
      return "正在等待另一位玩家…";
    },
  },

  async mounted() {
    this.token = localStorage.getItem("pokerhub_token") || "";
    await this.loadRules();
    this.syncPlayChrome();
    if (!this.token) return;
    await this.loadMe();
    if (!this.user) return;
    this.panel = "profile";
    this.connect().catch(() => {});
  },

  updated() {
    this.syncPlayChrome();
  },

  methods: {
    note(text, isError) {
      this.message = text;
      this.messageBad = Boolean(isError);
    },

    requireLogin() {
      if (this.token && this.user) return true;
      this.panel = "login";
      this.note("请先注册或登录", true);
      return false;
    },

    async api(path, options) {
      const headers = { "Content-Type": "application/json" };
      if (this.token) headers.Authorization = "Bearer " + this.token;
      const response = await fetch(path, Object.assign({ headers }, options || {}));
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = data.detail;
        throw new Error(typeof detail === "string" ? detail : "请求失败");
      }
      return data;
    },

    async loadMe() {
      try {
        this.user = await this.api("/api/me");
        this.form.nickname = this.user.nickname;
      } catch (error) {
        this.token = "";
        localStorage.removeItem("pokerhub_token");
        this.user = null;
        this.panel = "login";
        this.note(error.message, true);
      }
    },

    async loadRules() {
      try {
        const path = this.rulesGame === "ddz" ? "/api/rules/ddz" : "/api/rules";
        this.rules = await this.api(path);
      } catch (error) {
        this.note(error.message, true);
      }
    },

    async showRules(game) {
      this.rulesGame = game;
      await this.loadRules();
    },

    async openPanel(name) {
      if (name !== "login" && name !== "rules" && !this.requireLogin()) return;
      this.panel = name;
      this.message = "";
      if (name === "profile") await this.loadMe();
      if (name === "rules") await this.loadRules();
      if (name === "deck") await this.loadDeck();
      if (name === "history") await this.loadHistory();
    },

    async submitAuth() {
      const path = this.authMode === "login" ? "/api/login" : "/api/register";
      try {
        const data = await this.api(path, {
          method: "POST",
          body: JSON.stringify({
            username: this.form.username,
            password: this.form.password,
          }),
        });
        this.keepLogin(data);
        this.form.password = "";
        this.panel = "profile";
        this.note(this.authMode === "login" ? "登录成功" : "注册成功，已给你一套默认牌组");
        this.connect().catch(() => {});
      } catch (error) {
        this.note(error.message, true);
      }
    },

    keepLogin(data) {
      this.token = data.token;
      this.user = data.user;
      this.form.nickname = data.user.nickname;
      localStorage.setItem("pokerhub_token", data.token);
    },

    async logout() {
      try {
        await this.api("/api/logout", { method: "POST" });
      } catch (error) {
        // 本地仍然退出。令牌失效与否，以服务器为准。
      }
      this.token = "";
      this.user = null;
      this.game = null;
      this.ddz = null;
      this.ddzPlayId = "";
      this.ddzSelection = [];
      this.rematchWaiting = false;
      this.stopDdzTimer();
      this.waiting = false;
      localStorage.removeItem("pokerhub_token");
      if (this.ws) this.ws.close();
      this.panel = "login";
      this.note("已退出");
    },

    async saveProfile() {
      try {
        this.user = await this.api("/api/me", {
          method: "PATCH",
          body: JSON.stringify({ nickname: this.form.nickname }),
        });
        this.note("昵称已保存");
      } catch (error) {
        this.note(error.message, true);
      }
    },

    async loadDeck() {
      try {
        const [cards, deck] = await Promise.all([
          this.api("/api/cards"),
          this.api("/api/me/deck"),
        ]);
        this.catalog = cards.slice().sort((a, b) => {
          if (b.point !== a.point) return b.point - a.point;
          return "SHDC".indexOf(a.suit) - "SHDC".indexOf(b.suit);
        });
        this.selected = deck.cards.slice();
      } catch (error) {
        this.note(error.message, true);
      }
    },

    toggleCard(code) {
      const index = this.selected.indexOf(code);
      if (index >= 0) {
        this.selected.splice(index, 1);
        return;
      }
      if (this.selected.length >= 10) {
        this.note("最多选择 10 张", true);
        return;
      }
      this.selected.push(code);
    },

    async saveDeck() {
      try {
        const data = await this.api("/api/me/deck", {
          method: "PUT",
          body: JSON.stringify({ cards: this.selected }),
        });
        this.selected = data.cards;
        this.note("牌组已保存，下一局生效");
      } catch (error) {
        this.note(error.message, true);
      }
    },

    async loadHistory() {
      try {
        this.history = await this.api("/api/me/history");
        if (this.user) await this.loadMe();
      } catch (error) {
        this.note(error.message, true);
      }
    },

    connect() {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) return Promise.resolve();
      if (this.connecting) return this.connecting;
      if (!this.token) return Promise.reject(new Error("请先登录"));

      this.connecting = new Promise((resolve, reject) => {
        const protocol = location.protocol === "https:" ? "wss" : "ws";
        const socket = new WebSocket(protocol + "://" + location.host + "/ws");
        this.ws = socket;
        socket.onopen = () => {
          socket.send(JSON.stringify({ type: "hello", token: this.token }));
          resolve();
        };
        socket.onmessage = (event) => this.onMessage(JSON.parse(event.data));
        socket.onerror = () => reject(new Error("连不上服务器"));
        socket.onclose = () => {
          if (this.ws === socket) this.ws = null;
        };
      });
      const pending = this.connecting;
      pending.finally(() => {
        if (this.connecting === pending) this.connecting = null;
      });
      return pending;
    },

    async send(payload) {
      await this.connect();
      this.ws.send(JSON.stringify(payload));
    },

    onMessage(message) {
      if (message.type === "ready") return;
      if (message.type === "waiting") {
        this.waiting = true;
        this.waitGame = message.game === "ddz" ? "ddz" : "poker";
        this.panel = "match";
        this.note(message.message);
        return;
      }
      if (message.type === "cancelled") {
        this.waiting = false;
        this.note(message.message);
        return;
      }
      if (message.type === "error") {
        this.note(message.message, true);
        return;
      }
      if (message.type === "rematch_waiting") {
        this.rematchWaiting = true;
        this.note(message.message);
        return;
      }
      if (message.type === "state") {
        this.guestPlayId = "";
        this.applyState(message);
        return;
      }
      if (message.type === "ddz_state") {
        this.ddzPlayId = "";
        this.applyDdz(message);
        return;
      }
      if (message.type === "hint") {
        this.applyHint(message.options);
      }
    },

    applyState(state) {
      const prev = this.game;
      this.waiting = false;
      this.game = state;
      this.panel = "game";
      this.message = "";
      this.rematchWaiting = false;
      if (state.status === "finished" && this.user) this.loadHistory();
      this.cueTable(prev, state, "poker");
      this.syncPlayChrome();
    },

    // ----- 斗地主 -----

    applyDdz(state, focus = true) {
      const prev = this.ddz;
      this.waiting = false;
      this.ddz = state;
      // focus 为 false 时不抢界面：免登录局的机器人是网页自己在催，
      // 玩家翻到规则页或历史页看东西时，不该被硬拉回牌桌。
      if (focus) this.panel = "ddz";
      this.message = "";
      this.rematchWaiting = false;
      // 出掉的牌要从选中的牌里去掉；轮不到自己就清空。
      this.ddzSelection = this.ddzSelection.filter((card) => state.you.hand.includes(card));
      this.ddzHintOptions = [];
      this.ddzHintIndex = 0;
      if (state.phase !== "playing" || !state.you.is_turn) this.ddzSelection = [];
      if (state.phase === "finished" && this.user) this.loadHistory();
      this.cueTable(prev, state, "ddz");
      this.scheduleDdzAdvance();
      this.syncPlayChrome();
    },

    stopDdzTimer() {
      if (this.ddzTimer) {
        clearTimeout(this.ddzTimer);
        this.ddzTimer = null;
      }
    },

    scheduleDdzAdvance() {
      // 免登录的时候没有 WebSocket，机器人由网页停一下催一次。
      // 不这么做的话，我一出完牌，两个机器人立刻跟着出，牌桌上什么都看不清。
      this.stopDdzTimer();
      const state = this.ddz;
      if (!this.ddzPlayId || !state || state.phase === "finished") return;
      if (state.you.is_turn || state.you.is_bid_turn) return;
      const pause = Math.max(60, (state.bot_pause || 1) * 1000);
      this.ddzTimer = setTimeout(async () => {
        this.ddzTimer = null;
        try {
          const next = await this.api("/api/test/ddz/" + this.ddzPlayId + "/advance", { method: "POST" });
          this.applyDdz(next, false);
        } catch (error) {
          this.note(error.message, true);
        }
      }, pause);
    },

    applyHint(options) {
      this.ddzHintOptions = options || [];
      if (!this.ddzHintOptions.length) {
        this.ddzSelection = [];
        this.note("没有能压过的牌，只能点「不出」", true);
        return;
      }
      // 连着点「提示」就在几种打法之间轮着换。
      const option = this.ddzHintOptions[this.ddzHintIndex % this.ddzHintOptions.length];
      this.ddzHintIndex = (this.ddzHintIndex + 1) % this.ddzHintOptions.length;
      this.ddzSelection = option.cards.slice();
      this.note("提示：" + option.label);
    },

    ddzPoint(card) {
      if (card === "BJ" || card === "RJ") return DDZ_POINT[card];
      return DDZ_POINT[card.slice(1)] || Number(card.slice(1));
    },
    ddzRank(card) {
      if (card === "BJ") return "小王";
      if (card === "RJ") return "大王";
      return card.slice(1);
    },
    // 手牌左上角位置很窄，大小王只写「小」「大」，下面配个「王」字。
    ddzCorner(card) {
      if (card === "BJ") return "小";
      if (card === "RJ") return "大";
      return card.slice(1);
    },
    ddzSuit(card) {
      if (card === "BJ" || card === "RJ") return "王";
      return SUIT[card[0]] || card[0];
    },
    ddzRed(card) {
      return card === "RJ" || card[0] === "H" || card[0] === "D";
    },
    ddzJoker(card) {
      return card === "BJ" || card === "RJ";
    },
    signed(value) {
      return value > 0 ? "+" + value : String(value);
    },

    async startDdzSolo() {
      Sfx.ensure();
      if (this.ddz && this.ddz.phase !== "finished" && this.ddz.mode === "solo") {
        this.panel = "ddz";
        this.message = "";
        return;
      }
      this.panel = "ddz";
      this.message = "";
      this.ddzSelection = [];
      this.stopDdzTimer();
      try {
        if (this.user) {
          this.ddzPlayId = "";
          await this.send({ type: "ddz_solo" });
          return;
        }
        const state = await this.api("/api/test/ddz", { method: "POST" });
        this.ddzPlayId = state.play_id;
        this.applyDdz(state);
      } catch (error) {
        this.note(error.message, true);
      }
    },

    async startDdzMatch() {
      if (!this.requireLogin()) return;
      this.panel = "match";
      this.waitGame = "ddz";
      try {
        await this.send({ type: "ddz_match" });
      } catch (error) {
        this.note(error.message, true);
      }
    },

    async ddzAct(payload) {
      if (this.ddzPlayId) {
        let path = "/api/test/ddz/" + this.ddzPlayId;
        let body;
        if (payload.type === "ddz_bid") {
          path += "/bid";
          body = JSON.stringify({ score: payload.score });
        } else if (payload.type === "ddz_play") {
          path += "/play";
          body = JSON.stringify({ cards: payload.cards });
        } else if (payload.type === "ddz_pass") {
          path += "/pass";
        } else {
          path += "/surrender";
        }
        this.applyDdz(await this.api(path, { method: "POST", body }));
        return;
      }
      await this.send(payload);
    },

    async ddzBid(score) {
      this.ddzSelection = [];
      try {
        await this.ddzAct({ type: "ddz_bid", score });
      } catch (error) {
        this.note(error.message, true);
      }
    },

    async ddzPlay() {
      if (!this.ddzSelection.length) {
        this.note("先点手牌选牌", true);
        return;
      }
      const cards = this.ddzSelection.slice();
      try {
        await this.ddzAct({ type: "ddz_play", cards });
        this.ddzSelection = [];
      } catch (error) {
        this.note(error.message, true);
      }
    },

    async ddzPass() {
      this.ddzSelection = [];
      try {
        await this.ddzAct({ type: "ddz_pass" });
      } catch (error) {
        this.note(error.message, true);
      }
    },

    async ddzHint() {
      try {
        if (this.ddzPlayId) {
          const data = await this.api("/api/test/ddz/" + this.ddzPlayId + "/hint", { method: "POST" });
          this.applyHint(data.options);
          return;
        }
        await this.send({ type: "ddz_hint" });
      } catch (error) {
        this.note(error.message, true);
      }
    },

    ddzToggle(card) {
      if (!this.ddz || this.ddz.phase !== "playing" || !this.ddz.you.is_turn) return;
      const index = this.ddzSelection.indexOf(card);
      if (index >= 0) this.ddzSelection.splice(index, 1);
      else this.ddzSelection.push(card);
    },

    ddzClear() {
      this.ddzSelection = [];
    },

    // 打完一局之后一键再来。人机局自己说了算，匹配局要大家都点头。
    async rematchDdz() {
      this.ddzSelection = [];
      this.ddzHintOptions = [];
      try {
        if (this.ddzPlayId) {
          await this.startDdzSolo();
          return;
        }
        this.rematchWaiting = true;
        this.note(this.ddz && this.ddz.mode === "match" ? "已点头，等其他人…" : "正在开新的一局…");
        await this.send({ type: "ddz_rematch" });
      } catch (error) {
        this.rematchWaiting = false;
        this.note(error.message, true);
      }
    },

    async rematchPoker() {
      try {
        if (this.guestPlayId) {
          this.rematchWaiting = false;
          await this.startSolo();
          return;
        }
        this.rematchWaiting = true;
        this.note(this.game && this.game.mode === "match" ? "已点头，等对面…" : "正在开新的一局…");
        await this.send({ type: "rematch" });
      } catch (error) {
        this.rematchWaiting = false;
        this.note(error.message, true);
      }
    },

    async act(payload) {
      if (this.guestPlayId) {
        let path = "/api/test/solo/" + this.guestPlayId;
        let body;
        if (payload.type === "draw") path += "/draw";
        else if (payload.type === "play") {
          path += "/play";
          body = JSON.stringify({ card: payload.card });
        } else path += "/surrender";
        this.applyState(await this.api(path, { method: "POST", body }));
        return;
      }
      await this.send(payload);
    },

    async startSolo() {
      Sfx.ensure();
      if (this.game && this.game.status === "playing" && this.game.mode === "solo") {
        this.panel = "game";
        this.message = "";
        this.syncPlayChrome();
        return;
      }
      this.panel = "game";
      this.message = "";
      try {
        if (this.user) {
          this.guestPlayId = "";
          await this.send({ type: "start_solo" });
          return;
        }
        const state = await this.api("/api/test/solo", { method: "POST" });
        this.guestPlayId = state.play_id;
        this.applyState(state);
      } catch (error) {
        this.note(error.message, true);
      }
    },

    async startMatch() {
      if (!this.requireLogin()) return;
      this.panel = "match";
      try {
        await this.send({ type: "start_match" });
      } catch (error) {
        this.note(error.message, true);
      }
    },

    async cancelMatch() {
      try {
        await this.send({ type: this.waitGame === "ddz" ? "ddz_cancel_match" : "cancel_match" });
      } catch (error) {
        this.note(error.message, true);
      }
    },

    async drawCard() {
      try {
        await this.act({ type: "draw" });
      } catch (error) {
        this.note(error.message, true);
      }
    },

    async playCard(card) {
      try {
        await this.act({ type: "play", card });
      } catch (error) {
        this.note(error.message, true);
      }
    },

    async surrender() {
      // 这个按钮两种玩法共用，看现在坐在哪张桌子上。
      const inDdz = this.panel === "ddz" && this.ddz && this.ddz.phase !== "finished";
      const inPoker = this.panel !== "ddz" && this.game && this.game.status === "playing";
      if (!inDdz && !inPoker) {
        this.note("当前没有进行中的对局", true);
        return;
      }
      const words = inDdz ? "确定认输？这一局会记为失败。" : "确定投降？这局会记为失败。";
      if (!window.confirm(words)) return;
      try {
        if (inDdz) {
          this.ddzSelection = [];
          await this.ddzAct({ type: "ddz_surrender" });
        } else {
          await this.act({ type: "surrender" });
        }
      } catch (error) {
        this.note(error.message, true);
      }
    },

    leaveTable() {
      this.panel = "login";
      this.syncPlayChrome();
    },
    toggleMute() {
      this.muted = !this.muted;
      Sfx.muted = this.muted;
      localStorage.setItem("pokerhub_mute", this.muted ? "1" : "0");
      if (!this.muted) Sfx.play("tick");
    },
    cueTable(prev, next, kind) {
      if (!prev) {
        Sfx.play("deal");
        return;
      }
      const finished = kind === "ddz"
        ? prev.phase !== "finished" && next.phase === "finished"
        : prev.status !== "finished" && next.status === "finished";
      if (finished) {
        const won = kind === "ddz" ? next.result && next.result.you_win : next.winner === "you";
        Sfx.play(won ? "win" : "lose");
        return;
      }
      const lines = freshLines(prev.log, next.log);
      const sound = soundFor(lines);
      const wasTurn = kind === "ddz"
        ? prev.you.is_turn || prev.you.is_bid_turn
        : prev.you.is_turn;
      const nowTurn = kind === "ddz"
        ? next.you.is_turn || next.you.is_bid_turn
        : next.you.is_turn;
      if (sound) Sfx.play(sound);
      else if (nowTurn && !wasTurn) Sfx.play("turn");
    },
    syncPlayChrome() {
      const wolf = this.panel === "ddz" && Boolean(this.ddz);
      const poker = this.panel === "game" && Boolean(this.game);
      document.body.classList.toggle("app-play", wolf || poker);
      document.body.classList.toggle("app-wolf", wolf);
      document.body.classList.toggle("app-poker", poker);
    },
    ddzRole(person) {
      if (!this.ddz || this.ddz.phase === "bidding") return "未定";
      return person.is_landlord ? "狼人" : "村民";
    },
    ddzPortraitClass(person) {
      if (!this.ddz || this.ddz.phase === "bidding") return "hood";
      return person.is_landlord ? "wolf" : "human";
    },
    ddzPortrait(person, slot) {
      if (!this.ddz || this.ddz.phase === "bidding") return "/art/hood.jpg";
      if (person.is_landlord) return "/art/werewolf.jpg";
      if (slot === "right") return "/art/elder.jpg";
      if (slot === "you") return "/art/villager.jpg";
      return this.ddz.you.is_landlord ? "/art/villager.jpg" : "/art/elder.jpg";
    },
    ddzLore(line) {
      return this.loreLine(line)
        .replaceAll("当地主", "化为狼人")
        .replaceAll("地主", "狼人")
        .replaceAll("农民", "村民");
    },
    loreName(name) {
      if (name === "电脑") return "灰烬看守";
      if (name === "游客") return "无名旅人";
      return name;
    },
    loreLine(line) {
      return String(line).replaceAll("电脑", "灰烬看守").replaceAll("游客", "无名旅人");
    },
    suitSymbol(suit) {
      return SUIT[suit] || suit;
    },
    rankOf(card) {
      return card.slice(1);
    },
    isRed(card) {
      return card[0] === "H" || card[0] === "D";
    },
    point(card) {
      const rank = card.slice(1);
      const named = { J: 11, Q: 12, K: 13, A: 14 };
      return named[rank] || Number(rank);
    },
  },
}).mount("#app");
