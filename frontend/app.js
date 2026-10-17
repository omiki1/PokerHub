// 网页只负责显示和提交动作。
// 能不能出牌、这一回合谁得分、整局谁赢，都等服务器的 WebSocket 消息。
const SUIT = { S: "♠", H: "♥", D: "♦", C: "♣" };

const { createApp } = Vue;

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
      catalog: [],
      selected: [],
      history: [],
      game: null,
      guestPlayId: "",
      waiting: false,
      ws: null,
      connecting: null,
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
  },

  async mounted() {
    this.token = localStorage.getItem("pokerhub_token") || "";
    await this.loadRules();
    if (!this.token) return;
    await this.loadMe();
    if (!this.user) return;
    this.panel = "profile";
    this.connect().catch(() => {});
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
        this.rules = await this.api("/api/rules");
      } catch (error) {
        this.note(error.message, true);
      }
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
      if (message.type === "state") {
        this.guestPlayId = "";
        this.applyState(message);
      }
    },

    applyState(state) {
      this.waiting = false;
      this.game = state;
      this.panel = "game";
      this.message = "";
      if (state.status === "finished" && this.user) this.loadHistory();
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
        await this.send({ type: "cancel_match" });
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
      if (!this.game || this.game.status !== "playing") {
        this.note("当前没有进行中的对局", true);
        return;
      }
      if (!window.confirm("确定投降？这局会记为失败。")) return;
      try {
        await this.act({ type: "surrender" });
      } catch (error) {
        this.note(error.message, true);
      }
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
