const pptxgen = require("pptxgenjs");

// ---------------------------------------------------------------- palette
const DARK = "16142B";
const DARK2 = "26205140".slice(0, 6); // unused guard
const IND = "3F3684";   // indigo primary
const IND_L = "6E63B8";
const TEAL = "17B8A6";
const AMBER = "D9832B";
const RED = "B8433A";
const INK = "1E1B33";
const MUTED = "6E6A85";
const TINT = "F3F2F8";
const TINT2 = "E9E7F3";
const WHITE = "FFFFFF";
const HEAD = "Cambria";
const BODY = "Calibri";

const W = 13.33, H = 7.5, M = 0.6, CW = 12.13;

let LANG = "en";
const t = (en, zh) => (LANG === "en" ? en : zh);

// ---------------------------------------------------------------- helpers
function bg(slide, color) {
  slide.background = { color };
}

function titleBar(slide, title, kicker) {
  slide.addText(title, {
    x: M, y: 0.34, w: CW, h: 0.55, fontFace: HEAD, fontSize: 27, bold: true,
    color: INK, align: "left", margin: 0, valign: "middle",
  });
  if (kicker) {
    slide.addText(kicker, {
      x: M, y: 0.92, w: CW, h: 0.36, fontFace: BODY, fontSize: 12, color: MUTED,
      align: "left", margin: 0, valign: "top",
    });
  }
}

function footer(slide, left, page) {
  slide.addText(left, {
    x: M, y: 6.98, w: 10.0, h: 0.3, fontFace: BODY, fontSize: 9, color: "A6A2B8", margin: 0,
  });
  slide.addText(String(page), {
    x: W - M - 0.6, y: 6.98, w: 0.6, h: 0.3, fontFace: BODY, fontSize: 9,
    color: "A6A2B8", align: "right", margin: 0,
  });
}

function panel(slide, x, y, w, h, fill, lineColor) {
  slide.addShape("roundRect", {
    x, y, w, h, fill: { color: fill }, rectRadius: 0.07,
    line: lineColor ? { color: lineColor, width: 1 } : { color: fill, width: 0.5 },
  });
}

function numDot(slide, x, y, n, fill, size) {
  const d = size || 0.34;
  slide.addShape("ellipse", { x, y, w: d, h: d, fill: { color: fill }, line: { color: fill, width: 0 } });
  slide.addText(String(n), {
    x, y, w: d, h: d, fontFace: BODY, fontSize: d > 0.32 ? 12 : 10, bold: true,
    color: WHITE, align: "center", valign: "middle", margin: 0,
  });
}

function tag(slide, x, y, w, text, fill, color) {
  slide.addText(text, {
    x, y, w, h: 0.26, shape: "roundRect", rectRadius: 0.06,
    fill: { color: fill }, fontFace: BODY, fontSize: 9, bold: true,
    color: color || WHITE, align: "center", valign: "middle", margin: 0,
  });
}

function tableOpts(colW, extra) {
  return Object.assign({
    x: M, w: CW, colW,
    border: { type: "solid", color: "DDDAE8", pt: 0.5 },
    fontFace: BODY, fontSize: 10, color: INK, valign: "middle",
    autoPage: false,
  }, extra || {});
}

function headRow(cells) {
  return cells.map((c) => ({
    text: c,
    options: { fill: { color: IND }, color: WHITE, bold: true, fontSize: 10, valign: "middle" },
  }));
}

function zebra(rows, startWhite) {
  return rows.map((r, i) =>
    r.map((c) => {
      const isObj = typeof c === "object" && c !== null;
      const text = isObj ? c.text : c;
      const opts = Object.assign(
        { fill: { color: (i % 2 === (startWhite ? 0 : 1)) ? WHITE : TINT } },
        isObj ? c.options || {} : {}
      );
      return { text, options: opts };
    })
  );
}

function sectionSlide(pres, num, title, sub, page) {
  const s = pres.addSlide();
  bg(s, DARK);
  s.addShape("ellipse", { x: 10.6, y: -1.4, w: 4.6, h: 4.6, fill: { color: "211D42" }, line: { width: 0 } });
  s.addShape("ellipse", { x: 11.9, y: 4.5, w: 2.6, h: 2.6, fill: { color: "1D1A38" }, line: { width: 0 } });
  s.addText(num, {
    x: M, y: 2.28, w: 2.0, h: 0.9, fontFace: HEAD, fontSize: 58, bold: true, color: TEAL, margin: 0,
  });
  s.addText(title, {
    x: M, y: 3.18, w: 9.4, h: 0.85, fontFace: HEAD, fontSize: 34, bold: true, color: WHITE, margin: 0,
  });
  s.addText(sub, {
    x: M, y: 4.06, w: 9.0, h: 0.9, fontFace: BODY, fontSize: 13.5, color: "B9B4D6", margin: 0,
  });
  s.addText(String(page), {
    x: W - M - 0.6, y: 6.98, w: 0.6, h: 0.3, fontFace: BODY, fontSize: 9, color: "6E6A95",
    align: "right", margin: 0,
  });
  return s;
}

function arrow(slide, x, y, w, color) {
  slide.addShape("line", {
    x, y, w, h: 0, line: { color: color || IND_L, width: 1.5, endArrowType: "triangle" },
  });
}

// ---------------------------------------------------------------- build
function buildDeck(lang, outFile) {
  LANG = lang;
  const pres = new pptxgen();
  pres.layout = "LAYOUT_WIDE";
  pres.author = "AiScientist";
  pres.title = t("AiScientist — Technical Specification", "AiScientist — 技术规格说明");

  const FOOT = t("AiScientist · Technical Specification · v0.2.0",
                 "AiScientist · 技术规格说明 · v0.2.0");
  let P = 0;
  const nextPage = () => ++P;

  // ============================================================ 1 TITLE
  {
    const s = pres.addSlide();
    bg(s, DARK);
    s.addShape("ellipse", { x: 9.5, y: -1.9, w: 6.2, h: 6.2, fill: { color: "201C40" }, line: { width: 0 } });
    s.addShape("ellipse", { x: 11.4, y: 3.9, w: 3.4, h: 3.4, fill: { color: "1C1936" }, line: { width: 0 } });
    s.addText("AiScientist", {
      x: M, y: 1.62, w: 9.0, h: 0.9, fontFace: HEAD, fontSize: 52, bold: true, color: WHITE, margin: 0,
    });
    s.addText(t("Technical Specification", "技术规格说明"), {
      x: M, y: 2.52, w: 9.0, h: 0.62, fontFace: HEAD, fontSize: 27, color: TEAL, margin: 0,
    });
    s.addText(
      t("Job state · context management · cross-server job submission · workflows · engineering choices — with a deep dive on the genetic-variant-annotation line",
        "作业状态设计 · 上下文管理 · 跨服务器作业提交 · 工作流与业务能力 · 技术选型 —— 并对在研的基因变异注释线做重点分析"),
      { x: M, y: 3.28, w: 8.4, h: 0.95, fontFace: BODY, fontSize: 14, color: "B9B4D6", margin: 0 }
    );
    const chips = [
      t("UCI HPC3 + eyeserver", "UCI HPC3 + eyeserver"),
      t("Qwen3.6-35B-A3B (vLLM)", "Qwen3.6-35B-A3B (vLLM)"),
      t("~26K LOC · 933 tests", "~26K 行 · 933 测试"),
    ];
    let cx = M;
    chips.forEach((c) => {
      const w = LANG === "en" ? 3.0 : 2.6;
      s.addText(c, {
        x: cx, y: 4.62, w, h: 0.38, shape: "roundRect", rectRadius: 0.09,
        fill: { color: "2A2551" }, line: { color: "3E3872", width: 1 },
        fontFace: BODY, fontSize: 10.5, color: "D8D4EE", align: "center", valign: "middle", margin: 0,
      });
      cx += w + 0.22;
    });
    s.addText(t("Privacy-first multi-agent bioinformatics research console · UCI Vision / Ocular-Biology Lab",
                "隐私优先的多智能体生物信息学研究平台 · UCI 视觉／眼科生物学实验室"), {
      x: M, y: 6.55, w: 11.0, h: 0.34, fontFace: BODY, fontSize: 10.5, color: "7D78A3", margin: 0,
    });
    nextPage();
  }

  // ============================================================ 2 AT A GLANCE
  {
    const s = pres.addSlide();
    titleBar(s, t("At a glance", "系统概览"),
      t("What the system is, and the four numbers that describe its shape",
        "系统是什么，以及描述其规模的四个数字"));

    const stats = [
      [t("2", "2"), t("tiers: web gateway ↔ HPC3", "层：Web 网关 ↔ HPC3"), TEAL],
      [t("7", "7"), t("preset research pipelines", "个预置研究流水线"), IND],
      [t("14", "14"), t("atomic rewritable skills", "个可重写原子技能"), AMBER],
      [t("933", "933"), t("offline tests, no cluster needed", "个离线测试，无需集群"), IND_L],
    ];
    let x = M;
    stats.forEach(([n, label, col]) => {
      const w = 2.86;
      panel(s, x, 1.46, w, 1.42, TINT);
      s.addText(n, { x: x + 0.16, y: 1.54, w: w - 0.32, h: 0.72, fontFace: HEAD, fontSize: 40, bold: true, color: col, margin: 0, valign: "middle" });
      s.addText(label, { x: x + 0.16, y: 2.26, w: w - 0.32, h: 0.54, fontFace: BODY, fontSize: 10.5, color: MUTED, margin: 0 });
      x += w + 0.23;
    });

    panel(s, M, 3.08, CW, 3.02, WHITE, "DDDAE8");
    s.addText(t("A researcher logs in through the browser, points the console at a dataset, and asks a scientific question in plain language. Behind the login the console SSHes to UCI HPC3, serves an open-weights LLM on a Slurm GPU via vLLM, and runs a role-based research lab — Principal Investigator → Scientist → Critic — that plans the work, executes real analysis tools, and streams back a citable, publication-shaped report.",
      "研究者通过浏览器登录，为控制台绑定数据集，然后用自然语言提出科学问题。登录之后，控制台 SSH 连接到 UCI HPC3，通过 Slurm GPU + vLLM 提供开源权重大模型服务，并运行一个角色化的「研究实验室」——首席研究员 → 科学家 → 评审——负责规划工作、调用真实分析工具，并流式返回可引用的、论文体例的报告。"), {
      x: M + 0.28, y: 3.28, w: CW - 0.56, h: 1.0, fontFace: BODY, fontSize: 12.5, color: INK, margin: 0, lineSpacing: 18,
    });

    const posture = [
      [t("Privacy-first", "隐私优先"), t("The LLM runs on UCI hardware; raw data never leaves campus. DataBoundaryGuard inspects every brief before a model call.", "大模型运行在 UCI 自有硬件上，原始数据不出校园。每次模型调用前由 DataBoundaryGuard 检查 prompt。")],
      [t("Heavy compute on HPC3", "重计算在 HPC3"), t("GPU inference, run_code, scanpy, VEP, LIRICAL, report render — all Singularity-contained Slurm jobs, never silently on the web host.", "GPU 推理、run_code、scanpy、VEP、LIRICAL、报告渲染——全部是 Singularity 容器化的 Slurm 作业，绝不悄悄跑在 Web 主机上。")],
      [t("Honest over impressive", "诚实优于好看"), t("Tools report failures instead of faking success; a deterministic guard refuses to let the Critic accept a step whose tool run errored.", "工具如实报告失败而不伪造成功；确定性守卫不允许评审「接受」一个工具调用出错的步骤。")],
    ];
    let cx2 = M + 0.28;
    posture.forEach(([h, b], i) => {
      const w = 3.75;
      numDot(s, cx2, 4.42, i + 1, [TEAL, IND, AMBER][i], 0.3);
      s.addText(h, { x: cx2 + 0.4, y: 4.4, w: w - 0.4, h: 0.32, fontFace: BODY, fontSize: 12, bold: true, color: INK, margin: 0, valign: "middle" });
      s.addText(b, { x: cx2, y: 4.8, w, h: 1.2, fontFace: BODY, fontSize: 10, color: MUTED, margin: 0, valign: "top", lineSpacing: 14 });
      cx2 += w + 0.16;
    });
    footer(s, FOOT, nextPage());
  }

  // ============================================================ 3 ARCHITECTURE
  {
    const s = pres.addSlide();
    titleBar(s, t("Two tiers, decoupled by SSH", "两层架构，通过 SSH 解耦"),
      t("The web console holds no GPU and no persistent copy of a dataset", "Web 控制台不持有 GPU，也不长期保存数据集副本"));

    // tier boxes
    panel(s, M, 1.5, 3.5, 1.4, TINT, "DDDAE8");
    s.addText(t("Browser", "浏览器"), { x: M + 0.2, y: 1.62, w: 3.1, h: 0.32, fontFace: BODY, fontSize: 13, bold: true, color: INK, margin: 0 });
    s.addText(t("Console UI · WebSocket event stream · plan-mode review · Stop", "控制台 UI · WebSocket 事件流 · 计划审阅模式 · 停止"), { x: M + 0.2, y: 1.96, w: 3.1, h: 0.9, fontFace: BODY, fontSize: 10, color: MUTED, margin: 0, valign: "top", lineSpacing: 13 });

    panel(s, M + 3.95, 1.5, 4.2, 1.4, "2A2551");
    s.addText(t("eyeserver — FastAPI gateway", "eyeserver —— FastAPI 网关"), { x: M + 4.15, y: 1.62, w: 3.8, h: 0.32, fontFace: BODY, fontSize: 13, bold: true, color: WHITE, margin: 0 });
    s.addText(t("accounts · chat history · uploads · orchestration · report assembly · Postgres", "账户 · 会话历史 · 上传 · 编排 · 报告装配 · Postgres"), { x: M + 4.15, y: 1.96, w: 3.8, h: 0.9, fontFace: BODY, fontSize: 10, color: "C3BEE0", margin: 0, valign: "top", lineSpacing: 13 });

    panel(s, M + 8.6, 1.5, 3.53, 1.4, TINT, "DDDAE8");
    s.addText(t("UCI HPC3 — Slurm", "UCI HPC3 —— Slurm"), { x: M + 8.8, y: 1.62, w: 3.1, h: 0.32, fontFace: BODY, fontSize: 13, bold: true, color: INK, margin: 0 });
    s.addText(t("vLLM GPU serve · run_code · scanpy · VEP · LIRICAL · scGPT · pandoc render", "vLLM GPU 服务 · run_code · scanpy · VEP · LIRICAL · scGPT · pandoc 渲染"), { x: M + 8.8, y: 1.96, w: 3.1, h: 0.85, fontFace: BODY, fontSize: 10, color: MUTED, margin: 0, valign: "top", lineSpacing: 13 });

    arrow(s, M + 3.55, 2.2, 0.35);
    arrow(s, M + 8.2, 2.2, 0.35);
    s.addText("HTTPS / WSS", { x: M + 2.98, y: 2.94, w: 1.55, h: 0.26, fontFace: BODY, fontSize: 8.5, color: MUTED, align: "center", valign: "middle", margin: 0 });
    s.addText(t("SSH + Duo", "SSH + Duo"), { x: M + 7.63, y: 2.94, w: 1.55, h: 0.26, fontFace: BODY, fontSize: 8.5, color: MUTED, align: "center", valign: "middle", margin: 0 });

    const rows = [
      headRow([t("Channel", "通道"), t("What crosses it", "传输内容"), t("Implementation", "实现")]),
      ...zebra([
        [t("Command channel", "命令通道"), t("shell commands, sbatch, squeue/sacct, file staging", "shell 命令、sbatch、squeue/sacct、文件投递"), "paramiko SSHExecutor · RemoteExecutor protocol"],
        [t("Inference tunnel", "推理隧道"), t("chat + tool calls + exact token counts + streamed tokens", "对话 + 工具调用 + 精确 token 计数 + 流式 token"), t("per-session local port forward to the GPU node; vLLM speaks the OpenAI wire API (POST /v1/chat/completions, GET /v1/models, plus its own /tokenize), so no vendor SDK is involved and the endpoint is swappable", "每会话本地端口转发到 GPU 节点；vLLM 对外说的是 OpenAI 的接口协议（POST /v1/chat/completions、GET /v1/models，外加它自己的 /tokenize），因此不依赖任何厂商 SDK，端点可整体替换")],
        [t("Data plane", "数据面"), t("resumable chunked upload streamed straight to dfs3b", "断点续传分块上传，直接落到 dfs3b"), t("SFTP put_file — the gateway keeps no copy", "SFTP put_file —— 网关不留副本")],
        [t("Result plane", "结果面"), t("figures / tables synced back; checkpoints stay on DFS", "图表同步回本地；检查点留在 DFS"), t("get_file + BIOAGENT_RESULT_JSON stdout marker", "get_file + BIOAGENT_RESULT_JSON 标准输出标记")],
      ], true),
    ];
    s.addTable(rows, tableOpts([2.2, 4.4, 5.53], { y: 3.38, rowH: 0.5, fontSize: 9.5 }));

    s.addText(t("Everything the gateway does to HPC3 goes through one RemoteExecutor interface — implemented by the real SSH session and by an in-process MockExecutor, which is why the entire flow runs offline in tests.",
      "网关对 HPC3 的一切操作都经由同一个 RemoteExecutor 接口——由真实 SSH 会话与进程内 MockExecutor 两种实现提供，这正是整条链路能够离线测试的原因。"), {
      x: M, y: 6.05, w: CW, h: 0.62, fontFace: BODY, fontSize: 10.5, italic: true, color: MUTED, margin: 0, valign: "top", lineSpacing: 14,
    });
    footer(s, FOOT, nextPage());
  }

  // ============================================================ 4 SECTION 01
  sectionSlide(pres, "01", t("Job state design", "作业状态设计"),
    t("Seven state objects, four lifetimes, one resumable run", "七个状态对象、四种生命周期、一次可恢复的运行"), nextPage());

  // ============================================================ 5 STATE OBJECTS
  {
    const s = pres.addSlide();
    titleBar(s, t("The state objects of one job", "一个作业涉及的状态对象"),
      t("State is deliberately layered by LIFETIME — not stuffed into one blob", "状态按「生命周期」分层，而不是塞进一个大对象"));

    const rows = [
      headRow([t("Object", "对象"), t("Holds", "承载内容"), t("Lifetime", "生命周期"), t("Where it lives", "存放位置")]),
      ...zebra([
        ["Connection", t("SSH executor, GPU allocation, tunnel port, model, event log, subscribers, per-conversation chat memory", "SSH 执行器、GPU 分配、隧道端口、模型、事件日志、订阅者、按会话的聊天记忆"), t("one login", "一次登录"), t("gateway RAM", "网关内存")],
        ["RunState", t("run_id, conversation_id, chat_stop event, plan_event / plan_value, engine kind", "run_id、conversation_id、停止事件、计划审阅事件与值、引擎类型"), t("one run", "一次运行"), t("gateway RAM", "网关内存")],
        ["HarnessContext", t("decisions dict (dataset), workspace path, tunnel port, model — threaded into every tool", "decisions 字典（数据集）、工作区路径、隧道端口、模型——贯穿每个工具"), t("one step", "一个步骤"), t("gateway RAM", "网关内存")],
        ["LabRound", t("round_no, step_index, step text, specialist, HarnessResult, CriticVerdict", "轮次号、步骤序号、步骤文本、专家、工具结果、评审裁定"), t("one step", "一个步骤"), t("in LabResult", "位于 LabResult 内")],
        ["LabResult", t("question, agenda, all rounds, converged, accepted_steps, final_answer", "问题、议程、全部轮次、是否收敛、通过步数、最终答案"), t("one run", "一次运行"), t("RAM → JSON", "内存 → JSON")],
        ["run_state.json", t("LabResult + guidance + dataset paths + case note — the resume record", "LabResult + 引导 + 数据集路径 + 病例记录——恢复所需的全部信息"), t("permanent", "永久"), "artifacts/process/"],
        ["JobRecord", t("job_id, kind, owner, Slurm state — written the instant sbatch returns", "job_id、类型、属主、Slurm 状态——sbatch 返回即写入"), t("until terminal", "直到终态"), t("atomic JSON registry", "原子 JSON 注册表")],
        [t("Postgres rows", "Postgres 记录"), t("User / Dataset / Run / Conversation / Message — metadata in DB, blobs on disk", "用户／数据集／运行／会话／消息——元数据入库，大文件留盘"), t("permanent", "永久"), "eyeserver Postgres"],
      ], true),
    ];
    s.addTable(rows, tableOpts([1.85, 5.55, 1.55, 2.18], { y: 1.38, rowH: 0.55, fontSize: 9 }));

    s.addText(t("Checkpoints (work/adata_*.h5ad, annotated TSVs) never come back to the gateway — they accumulate in place on shared DFS so the next step's Slurm job reads them without a round trip.",
      "检查点（work/adata_*.h5ad、注释后的 TSV）从不回传网关——它们就地累积在共享 DFS 上，下一步的 Slurm 作业直接读取，无需往返。"), {
      x: M, y: 6.42, w: CW, h: 0.5, fontFace: BODY, fontSize: 10, italic: true, color: MUTED, margin: 0,
    });
    footer(s, FOOT, nextPage());
  }

  // ============================================================ 6 LIFECYCLE
  {
    const s = pres.addSlide();
    titleBar(s, t("The lifecycle of one job", "一个作业的完整生命周期"),
      t("Two human-in-the-loop gates: the Duo push, and plan-mode review", "两个人在环节点：Duo 推送认证，以及计划审阅模式"));

    const steps = [
      [t("Connect", "连接"), t("SSH + Duo, then one-shot GPU provisioning: connecting → provisioning → ready. A session is never handed back with SSH up but no model.", "SSH + Duo，随后一次性完成 GPU 供给：connecting → provisioning → ready。绝不会返回「SSH 已通但没有模型」的半连接会话。")],
      [t("Plan", "规划"), t("The PI turns the question into an ordered agenda (≤20 steps). Plan mode pauses on plan_event until the user approves or edits it.", "首席研究员把问题拆成有序议程（≤20 步）。计划模式在 plan_event 上阻塞，直到用户批准或编辑。")],
      [t("Execute", "执行"), t("Per step: pre-flight gate → Scientist calls tools → Critic returns accept | revise + score. A deterministic guard blocks accepting a failed tool run.", "每个步骤：预检门 → 科学家调用工具 → 评审给出 accept | revise 与评分。确定性守卫禁止「接受」失败的工具调用。")],
      [t("Converge", "收敛"), t("Round budget derives from the agenda so every planned step runs; each step is capped at max_revisions before force-advance.", "轮次预算由议程推导，保证每个计划步骤都能执行；每步最多 max_revisions 次修订后强制推进。")],
      [t("Report", "报告"), t("A deterministic, no-AI assembly (pandoc → PDF/DOCX) embeds the exact figures and tables the tools produced.", "确定性、无 AI 参与的装配（pandoc → PDF/DOCX），嵌入工具实际产出的图与表。")],
      [t("Persist", "持久化"), t("run_state.json is written so the run can be resumed, or its report regenerated, without re-running the analysis.", "写入 run_state.json，使运行可被恢复、报告可被重生成，而无需重跑分析。")],
    ];
    let x = M;
    const w = 1.9;
    steps.forEach(([h, b], i) => {
      panel(s, x, 1.5, w, 4.0, i % 2 === 0 ? TINT : WHITE, "DDDAE8");
      numDot(s, x + 0.14, 1.66, i + 1, i < 2 ? TEAL : i < 4 ? IND : AMBER, 0.32);
      s.addText(h, { x: x + 0.12, y: 2.08, w: w - 0.24, h: 0.34, fontFace: BODY, fontSize: 12.5, bold: true, color: INK, margin: 0 });
      s.addText(b, { x: x + 0.12, y: 2.44, w: w - 0.24, h: 2.9, fontFace: BODY, fontSize: 9.5, color: MUTED, margin: 0, valign: "top", lineSpacing: 12.5 });
      if (i < steps.length - 1) arrow(s, x + w + 0.02, 3.5, 0.16);
      x += w + 0.14;
    });

    panel(s, M, 5.72, CW, 0.96, TINT2);
    s.addText([
      { text: t("Resume without re-planning  ", "无需重新规划的恢复  "), options: { bold: true, color: INK } },
      { text: t("ResumeState.from_run_state loads the persisted run, keeps every ACCEPTED round as a read-only finding, always re-runs the changed step, and then lets the PI evaluate which LATER steps actually depend on the change — an independent literature step is kept, a downstream DE step is re-run. Checkpoints on DFS make this cheap.",
        "ResumeState.from_run_state 载入已持久化的运行，把所有「已通过」的轮次作为只读结论保留，必然重跑被修改的步骤，再由首席研究员判断哪些后续步骤真正依赖该改动——独立的文献步骤予以保留，下游的差异表达步骤则重跑。DFS 上的检查点让这一切代价很低。"), options: { color: MUTED } },
    ], { x: M + 0.24, y: 5.84, w: CW - 0.48, h: 0.74, fontFace: BODY, fontSize: 10.5, margin: 0, valign: "top", lineSpacing: 14 });
    footer(s, FOOT, nextPage());
  }

  // ============================================================ 7 ISOLATION
  {
    const s = pres.addSlide();
    titleBar(s, t("Isolation, cancellation, durability", "隔离、取消与持久性"),
      t("Why run state is per-RUN and not per-connection", "为什么运行状态按「运行」而不是按「连接」划分"));

    const cards = [
      [t("Per-run isolation", "按运行隔离"), TEAL, [
        t("One SSH/GPU Connection is shared across a user's windows, tabs and conversations.", "一个 SSH/GPU 连接会被用户的多个窗口、标签页与会话共享。"),
        t("Each RUN gets its own cancel + plan-review events and its own identity (run_id + conversation_id).", "每个「运行」拥有自己的取消事件、计划审阅事件，以及自己的身份（run_id + conversation_id）。"),
        t("Every streamed WS event is tagged, so the client demuxes it into the OWNING conversation bubble.", "每条流式 WS 事件都带标签，客户端据此把它分发回「所属」的会话气泡。"),
      ]],
      [t("Cancellation that actually lands", "真正生效的取消"), IND, [
        t("Runs on one connection are serialized — /api/lab returns 409 while a run is live.", "同一连接上的运行是串行的——运行进行中 /api/lab 返回 409。"),
        t("Stop sets the run's own chat_stop; streaming loops and the step callback poll it, and the in-flight Slurm job is scancel-led.", "停止会置位该运行自身的 chat_stop；流式循环与步骤回调轮询它，并对在飞的 Slurm 作业执行 scancel。"),
        t("A stale approve/cancel naming a finished run resolves to a no-op instead of hitting whatever run is live now.", "指向已结束运行的过期批准／取消会解析为空操作，而不会误伤当前正在运行的作业。"),
      ]],
      [t("Surviving a restart", "重启后的存活"), AMBER, [
        t("Slurm owns the job the moment sbatch returns — the compute keeps running even if the gateway process dies.", "sbatch 一返回，作业就归 Slurm 所有——即使网关进程崩溃，计算仍在继续。"),
        t("Every submission is written to an atomic JSON registry the instant the job_id is known.", "每次提交在拿到 job_id 的瞬间就写入原子 JSON 注册表。"),
        t("A reconnecting session looks up its still-incomplete jobs and reattaches — reattach is ownership-guarded.", "重连的会话查出自己尚未完成的作业并重新挂接——重新挂接受属主校验保护。"),
      ]],
    ];
    let x = M;
    cards.forEach(([h, col, items]) => {
      const w = 3.87;
      panel(s, x, 1.46, w, 4.5, WHITE, "DDDAE8");
      s.addShape("roundRect", { x, y: 1.46, w, h: 0.5, fill: { color: col }, rectRadius: 0.07, line: { color: col, width: 1 } });
      s.addShape("rect", { x, y: 1.78, w, h: 0.18, fill: { color: col }, line: { color: col, width: 1 } });
      s.addText(h, { x: x + 0.2, y: 1.5, w: w - 0.4, h: 0.42, fontFace: BODY, fontSize: 13, bold: true, color: WHITE, margin: 0, valign: "middle" });
      s.addText(items.map((it, i) => ({ text: it, options: { bullet: true, breakLine: i < items.length - 1, paraSpaceAfter: 8 } })), {
        x: x + 0.2, y: 2.14, w: w - 0.4, h: 3.6, fontFace: BODY, fontSize: 10.5, color: INK, margin: 0, valign: "top", lineSpacing: 14,
      });
      x += w + 0.16;
    });

    s.addText(t("The isolation refactor came out of a real defect: run state keyed only by connection_id produced cross-window plan leaks, cross-cancels and stale replans.",
      "这次隔离重构源于一个真实缺陷：运行状态仅以 connection_id 为键，导致跨窗口的计划泄漏、误取消与过期重规划。"), {
      x: M, y: 6.15, w: CW, h: 0.5, fontFace: BODY, fontSize: 10.5, italic: true, color: MUTED, margin: 0,
    });
    footer(s, FOOT, nextPage());
  }

  // ============================================================ 8 SECTION 02
  sectionSlide(pres, "02", t("Context management", "上下文管理"),
    t("Two paths, one budget discipline, total degradation", "两条路径、同一套预算纪律、彻底的降级策略"), nextPage());

  // ============================================================ 9 TWO PATHS
  {
    const s = pres.addSlide();
    titleBar(s, t("Two context paths, one estimator", "两条上下文路径，共用一套估算器"),
      t("A research step can afford prefill latency; a chat turn cannot", "研究步骤可以承受 prefill 延迟，聊天轮次不能"));

    const cols = [
      [t("Research path — research_harness", "研究路径 —— research_harness"), IND, [
        [t("Prompt shape", "Prompt 结构"), t("system + tool schemas + running tool-call transcript", "system + 工具 schema + 持续累积的工具调用转录")],
        [t("Budget", "预算"), t("the FULL served window (BIOAGENT_VLLM_MAX_MODEL_LEN, 131072 in prod) − 2048 output reserve − 2048 safety margin", "使用完整服务窗口（BIOAGENT_VLLM_MAX_MODEL_LEN，生产为 131072）− 2048 输出预留 − 2048 安全余量")],
        [t("Counting", "计数"), t("exact count from vLLM /tokenize on the GPU node when available; else a char heuristic at 2.6 chars/token — deliberately an OVERCOUNT", "可用时取 GPU 节点上 vLLM /tokenize 的精确计数；否则按 2.6 字符/token 的启发式估算——刻意高估")],
        [t("Overflow", "溢出处理"), t("reactive self-compaction: when vLLM returns an HTTP 400 for context length, re-trim with +3072 extra reserve and retry, up to 3 times", "反应式自压缩：当 vLLM 因上下文超限返回 HTTP 400 时，追加 3072 预留重新裁剪并重试，最多 3 次")],
      ]],
      [t("Fast chat path — chat_context", "快速聊天路径 —— chat_context"), TEAL, [
        [t("Prompt shape", "Prompt 结构"), t("system + rolling summary of older turns + last 6 exchanges VERBATIM + question", "system + 旧轮次的滚动摘要 + 最近 6 组对话原文 + 当前问题")],
        [t("Budget", "预算"), t("a SELF-IMPOSED 24K ceiling, not the 131K window — prefill scales with prompt length and would destroy first-token latency", "自设 24K 上限，而不是 131K 窗口——prefill 随 prompt 长度增长，会摧毁首 token 延迟")],
        [t("Compaction", "压缩"), t("evicted turns are SUMMARIZED, not dropped; re-summarization is INCREMENTAL (prior summary + only newly-evicted turns) so cost stays flat", "被淘汰的轮次被「摘要」而非「丢弃」；重摘要是增量的（上次摘要 + 仅新淘汰的轮次），成本恒定")],
        [t("Placement", "放置方式"), t("memory is spliced INTO the system message — Qwen's chat template raises on a second system message, so vLLM rejects the whole turn with an HTTP 400 (verified by rendering the staged template both ways)", "记忆被拼接进 system 消息内——Qwen 的 chat 模板在出现第二条 system 消息时会抛错，vLLM 会以 HTTP 400 拒绝整轮请求（用线上模板双向渲染验证过）")],
      ]],
    ];
    let x = M;
    cols.forEach(([h, col, rows]) => {
      const w = 5.98;
      panel(s, x, 1.44, w, 4.55, WHITE, "DDDAE8");
      s.addShape("roundRect", { x, y: 1.44, w, h: 0.5, fill: { color: col }, rectRadius: 0.07, line: { color: col, width: 1 } });
      s.addShape("rect", { x, y: 1.76, w, h: 0.18, fill: { color: col }, line: { color: col, width: 1 } });
      s.addText(h, { x: x + 0.22, y: 1.48, w: w - 0.44, h: 0.42, fontFace: BODY, fontSize: 13, bold: true, color: WHITE, margin: 0, valign: "middle" });
      let y = 2.1;
      rows.forEach(([k, v]) => {
        s.addText(k, { x: x + 0.22, y, w: 1.55, h: 0.9, fontFace: BODY, fontSize: 10, bold: true, color: col === TEAL ? "0E8577" : IND, margin: 0, valign: "top" });
        s.addText(v, { x: x + 1.82, y, w: w - 2.04, h: 0.92, fontFace: BODY, fontSize: 9.8, color: INK, margin: 0, valign: "top", lineSpacing: 12.5 });
        y += 0.98;
      });
      x += w + 0.17;
    });

    s.addText(t("Both paths emit the SAME event vocabulary — context_measured / context_trimmed — so one renderer serves both, and the console shows a live \"18.4K / 24K\" occupancy chip. Every failure mode of compaction (no summarizer, an exception, a junk reply, a token counter returning None) degrades to dropping the oldest turns: context management must never be the thing that breaks a turn.",
      "两条路径发出同一套事件词汇——context_measured / context_trimmed——因此一个渲染器即可服务两者，控制台可实时显示「18.4K / 24K」占用标记。压缩的每一种失败模式（没有摘要器、抛出异常、返回垃圾、token 计数返回 None）都降级为丢弃最旧轮次：上下文管理绝不能成为让一轮对话失败的原因。"), {
      x: M, y: 6.12, w: CW, h: 0.72, fontFace: BODY, fontSize: 10.5, italic: true, color: MUTED, margin: 0, valign: "top", lineSpacing: 14,
    });
    footer(s, FOOT, nextPage());
  }

  // ============================================================ 10 GROUNDING
  {
    const s = pres.addSlide();
    titleBar(s, t("Beyond the window: what else controls context", "窗口之外：还有什么在管控上下文"),
      t("Progressive disclosure, grounding, agent memory and the privacy boundary", "渐进式披露、事实锚定、智能体记忆与隐私边界"));

    const items = [
      [t("Progressive disclosure of skills", "技能的渐进式披露"), TEAL,
        t("The tool registry stays at ~10 always-visible tools. The 14 atomic skills are surfaced as a SHORT manifest (name + one line); the Scientist fetches the full body with read_skill_reference, and finds the right one with search_skills. Three levels: manifest → guidance → code.",
          "工具注册表保持在约 10 个常驻工具。14 个原子技能仅以「短清单」（名称 + 一行说明）出现；科学家通过 read_skill_reference 取回完整正文，通过 search_skills 检索合适的技能。三级披露：清单 → 指导 → 代码。")],
      [t("Scoped briefs (DAG planner)", "作用域化的任务简报（DAG 规划器）"), IND,
        t("Under the DAG planner each node gets a brief scoped to ONE task — \"do ONLY this task; earlier outputs already exist\". That structurally cures the double-QC and step-1-runs-everything failure modes, and keeps each node's prompt small.",
          "在 DAG 规划器下，每个节点收到限定于「单一任务」的简报——「只做这个任务；先前的产出已经存在」。这从结构上根治了重复 QC 与「第一步把活全干了」的失败模式，同时让每个节点的 prompt 保持精简。")],
      [t("Closed-set grounding", "闭集事实锚定"), AMBER,
        t("Before the report is written the lab compiles a controlled vocabulary, the methods actually performed, and a fact table from accepted rounds. After generation, verify_report_facts deterministically corrects a wrong assembly or a fabricated \"0 non-PASS\" regardless of what the model wrote.",
          "在撰写报告之前，实验室会汇编一份受控词表、实际执行过的方法，以及来自已通过轮次的事实表。生成之后，verify_report_facts 会确定性地纠正错误的参考基因组或伪造的「0 个非 PASS」，无论模型写了什么。")],
      [t("Per-agent evolving memory  ·  OFF by default", "按智能体演化的记忆 · 默认关闭"), IND_L,
        t("Built and unit-tested (agents/agent_memory.py): a private, disk-backed store per specialist — episodes.jsonl plus a distilled lessons.md on the gateway disk, recalled into the brief and reflected on at end of run. DOUBLE-GATED: it needs BIOAGENT_AGENT_MEMORY=1 AND the DAG planner, and the flag is not in the deployed .env — so it has never run in production. Listed as designed capability, not as shipped behaviour.",
          "已实现并有单元测试（agents/agent_memory.py）：每位专家在网关磁盘上有私有存储——episodes.jsonl 加提炼后的 lessons.md，在简报中召回、运行结束时反思。但是双重开关：需要 BIOAGENT_AGENT_MEMORY=1 且启用 DAG 规划器，而该开关并不在已部署的 .env 中——因此它从未在生产环境跑过。此处作为「已设计的能力」列出，而非「已上线的行为」。")],
      [t("DataBoundaryGuard", "数据边界守卫"), RED,
        t("Inspects the brief before ANY model call. Secrets are always blocked. Raw tabular data is blocked when the endpoint is remote and allowed when the LLM is this session's local tunneled Qwen — and raw-data detection is SOURCE-SCOPED to the user-provided spans, so system-built scaffolding is never sniffed.",
          "在任何模型调用之前检查 prompt。密钥永远阻断。当端点是远程时阻断原始表格数据，当大模型是本会话本地隧道内的 Qwen 时放行——且原始数据检测「按来源限定」于用户提供的片段，系统自建的脚手架文本不会被误判。")],
      [t("Reply reserve", "回复预留"), MUTED,
        t("A single-shot PI / Critic / synthesize reply is guaranteed 8192 tokens of output room. When the prompt is small the reply may use everything left over, so a long manuscript is never capped mid-sentence.",
          "单次的首席研究员／评审／综合生成回复被保证 8192 token 的输出空间。当 prompt 较小时，回复可以用尽剩余窗口，因此长篇稿件不会被中途截断。")],
    ];
    let x = M, y = 1.44;
    items.forEach(([h, col, b], i) => {
      const w = 3.87, hh = 2.3;
      panel(s, x, y, w, hh, TINT);
      s.addShape("ellipse", { x: x + 0.18, y: y + 0.2, w: 0.26, h: 0.26, fill: { color: col }, line: { color: col, width: 1 } });
      s.addText(h, { x: x + 0.54, y: y + 0.14, w: w - 0.74, h: 0.4, fontFace: BODY, fontSize: 11.5, bold: true, color: INK, margin: 0, valign: "middle" });
      s.addText(b, { x: x + 0.18, y: y + 0.6, w: w - 0.36, h: 1.58, fontFace: BODY, fontSize: 9.5, color: MUTED, margin: 0, valign: "top", lineSpacing: 12.5 });
      if (i % 3 === 2) { x = M; y += hh + 0.22; } else { x += w + 0.16; }
    });
    footer(s, FOOT, nextPage());
  }

  // ============================================================ 11 SECTION 03
  sectionSlide(pres, "03", t("Cross-server job submission", "跨服务器作业提交"),
    t("How the web gateway gets real compute out of a shared Slurm cluster", "Web 网关如何从共享 Slurm 集群中获得真实算力"), nextPage());

  // ============================================================ 12 TRANSPORT
  {
    const s = pres.addSlide();
    titleBar(s, t("Transport, authentication, containment", "传输、认证与容器化"),
      t("Three constraints from RCIC policy shaped this design", "RCIC 的三条约束塑造了这套设计"));

    const rows = [
      headRow([t("Concern", "关注点"), t("Constraint", "约束"), t("Design answer", "设计应对")]),
      ...zebra([
        [t("Login", "登录"), t("HPC3 password login requires an interactive Duo push; users must be on the campus network or VPN", "HPC3 密码登录需要交互式 Duo 推送；用户必须在校园网或 VPN 内"),
          t("paramiko keyboard-interactive with a duo_callback — the gateway thread blocks on duo_event until the UI posts the chosen method. HPC3 credentials are NEVER stored; only a bcrypt hash of the APP password lives in the DB.", "paramiko 的 keyboard-interactive 配合 duo_callback——网关线程在 duo_event 上阻塞，直到 UI 回传所选方式。HPC3 凭据从不落库；数据库里只保存「应用」口令的 bcrypt 哈希。")],
        [t("Group permissions", "组权限"), t("Paths under /dfs3b/ruic20_lab/ require an active ruic20_hpc UNIX group", "/dfs3b/ruic20_lab/ 下的路径需要激活 ruic20_hpc 用户组"),
          t("wrap_in_group base64-encodes the whole command and pipes it into sg <group> -c 'bash -s', so heredocs and quoting survive untouched.", "wrap_in_group 将整条命令 base64 编码后管道送入 sg <group> -c 'bash -s'，使 heredoc 与引号原样保留。")],
        [t("GPU serving", "GPU 服务"), t("Nodes are shared, so a fixed vLLM port would collide with another user's serve", "节点是共享的，固定的 vLLM 端口会与他人的服务冲突"),
          t("The serve job picks a FREE port and records it per job id at $HOME/.bioagent/vllm.<jobid>.port; the gateway reads it back and opens a local forward. Jobs are per-user named and isolated via squeue --me.", "服务作业自行选取空闲端口，并按作业 ID 记录到 $HOME/.bioagent/vllm.<jobid>.port；网关读回后建立本地转发。作业按用户命名，并通过 squeue --me 隔离。")],
        [t("Containment", "容器隔离"), t("Heavy work must not run on the web host; contained code must not be able to damage raw data", "重负载不得跑在 Web 主机上；容器内代码不得损坏原始数据"),
          t("Every job is a Singularity exec with --containall: the dataset is bind-mounted READ-ONLY, only work/ and artifacts/ are writable, and every other host path is hidden.", "每个作业都是带 --containall 的 Singularity exec：数据集以只读方式绑定挂载，仅 work/ 与 artifacts/ 可写，其余主机路径全部隐藏。")],
        [t("Code freshness", "代码时效"), t("Rebuilding a .sif for every tool edit is untenable (HPC3 has no fakeroot)", "每次改工具就重建 .sif 不可行（HPC3 无 fakeroot）"),
          t("The live source dir on dfs3b is bind-mounted read-only and put on PYTHONPATH, so editing a tool only needs a code sync — never an image rebuild.", "dfs3b 上的实时源码目录以只读绑定挂载并加入 PYTHONPATH，改工具只需同步代码——无需重建镜像。")],
      ], true),
    ];
    s.addTable(rows, tableOpts([1.5, 3.9, 6.73], { y: 1.42, rowH: 0.68, fontSize: 9.5 }));
    footer(s, FOOT, nextPage());
  }

  // ============================================================ 13 SLURM LIFECYCLE
  {
    const s = pres.addSlide();
    titleBar(s, t("The Slurm batch lifecycle", "Slurm 批作业生命周期"),
      t("The hard part on a shared cluster is not running the job — it is the QUEUE", "共享集群上的难点不是运行作业，而是排队"));

    const flow = [
      [t("submit", "提交"), t("sbatch a generated #!/bin/bash script; job_id recorded to the registry immediately", "sbatch 提交生成的 #!/bin/bash 脚本；job_id 立即写入注册表")],
      [t("wait PD → R", "等待 PD → R"), t("if it never starts within startup_timeout_s: scancel and RESUBMIT, bounded by max_attempts — a long queue is not a hard failure", "若在 startup_timeout_s 内未启动：scancel 并重新提交，受 max_attempts 约束——排队久不算硬失败")],
      [t("wait COMPLETE", "等待完成"), t("run_timeout_s derived from the SBATCH --time plus a margin; a Stop request scancels and raises JobCancelled, distinct from a real failure", "run_timeout_s 由 SBATCH --time 加余量推导；用户点击停止将 scancel 并抛出 JobCancelled，与真实失败区分开")],
      [t("conclude", "判定"), t("squeue and sacct are eventually-consistent, so we conclude ONLY on a terminal sacct state (with a gone-confirm fallback for clusters without accounting)", "squeue 与 sacct 是最终一致的，因此只在 sacct 达到终态时判定（对未开启 accounting 的集群提供「消失确认」兜底）")],
    ];
    let x = M;
    flow.forEach(([h, b], i) => {
      const w = 2.86;
      panel(s, x, 1.44, w, 2.05, i === 0 ? TINT2 : TINT);
      numDot(s, x + 0.16, 1.6, i + 1, IND, 0.3);
      s.addText(h, { x: x + 0.54, y: 1.56, w: w - 0.7, h: 0.34, fontFace: BODY, fontSize: 12, bold: true, color: INK, margin: 0, valign: "middle" });
      s.addText(b, { x: x + 0.16, y: 1.98, w: w - 0.32, h: 1.4, fontFace: BODY, fontSize: 9.5, color: MUTED, margin: 0, valign: "top", lineSpacing: 12.5 });
      if (i < 3) arrow(s, x + w + 0.02, 2.46, 0.19);
      x += w + 0.23;
    });

    panel(s, M, 3.68, 5.9, 2.55, WHITE, "DDDAE8");
    s.addText(t("The call contract", "调用契约"), { x: M + 0.24, y: 3.8, w: 5.4, h: 0.34, fontFace: BODY, fontSize: 13, bold: true, color: IND, margin: 0 });
    const contract = [
      t("Arguments go in via a heredoc sentinel, never a shell-escaped argv", "参数经由 heredoc 哨兵传入，绝不走 shell 转义的 argv"),
      t("The job prints its result as one line prefixed BIOAGENT_RESULT_JSON", "作业以 BIOAGENT_RESULT_JSON 前缀的单行输出返回结果"),
      t("Entrypoints per line: scrna_cli · variant_cli · phenotype_cli · paperqa_cli", "各条线的入口：scrna_cli · variant_cli · phenotype_cli · paperqa_cli"),
      t("Small figures/tables are synced back; checkpoints stay on DFS", "小体积图表同步回本地；检查点留在 DFS"),
      t("Optional per-run memoization stops a re-called tool from clobbering good tables", "可选的按运行记忆化，避免工具被重复调用后覆盖已有的好结果"),
    ];
    s.addText(contract.map((c, i) => ({ text: c, options: { bullet: true, breakLine: i < contract.length - 1, paraSpaceAfter: 6 } })), {
      x: M + 0.24, y: 4.18, w: 5.45, h: 1.95, fontFace: BODY, fontSize: 10, color: INK, margin: 0, valign: "top", lineSpacing: 13.5,
    });

    panel(s, M + 6.14, 3.68, 5.99, 2.55, WHITE, "DDDAE8");
    s.addText(t("Graceful degradation", "优雅降级"), { x: M + 6.38, y: 3.8, w: 5.5, h: 0.34, fontFace: BODY, fontSize: 13, bold: true, color: TEAL, margin: 0 });
    s.addText(t("If no live remote is wired — offline, mock mode, or HPC disabled — every offload executor FALLS BACK to running the same tool in-process. A run never hard-fails just because the cluster is unavailable, and the local test suite exercises that path unchanged. The trade-off is explicit and logged: the REST variant path, for example, announces loudly that it is capped at the first 500 variants rather than silently reporting a truncated WGS VCF as a small cohort.",
      "若没有可用的远端连接——离线、mock 模式或 HPC 被关闭——每个下发执行器都会「回落」为在本进程内运行同一个工具。运行不会仅因集群不可用而硬失败，本地测试套件也正是走这条路径。这个折中是显式且有日志的：例如 REST 变异路径会明确声明自己被限制在前 500 个变异，而不是悄悄把被截断的 WGS VCF 报告成一个小队列。"), {
      x: M + 6.38, y: 4.18, w: 5.5, h: 1.95, fontFace: BODY, fontSize: 10, color: INK, margin: 0, valign: "top", lineSpacing: 13.5,
    });
    footer(s, FOOT, nextPage());
  }

  // ============================================================ 14 SECTION 04
  sectionSlide(pres, "04", t("Workflows & business capability", "工作流与业务能力"),
    t("Three layers: fixed registry · atomic skills · preset pipelines", "三层结构：固定注册表 · 原子技能 · 预置流水线"), nextPage());

  // ============================================================ 15 THREE LAYERS
  {
    const s = pres.addSlide();
    titleBar(s, t("Three layers of capability", "能力的三个层次"),
      t("\"Skill\" is reserved for the atomic, rewritable layer — the naming is load-bearing", "「技能」一词专指原子、可重写的那一层——这个命名是有承重作用的"));

    const layers = [
      [t("Registry — fixed core tools", "注册表 —— 固定核心工具"), IND, "agents/registry.py",
        t("~10 always-visible, typed tools with JSON schemas: the analysis line, annotate_variants, map_phenotype_to_hpo, run_lirical, literature_search, deep_literature, scgpt_annotate, inspect_dataset, schematic, run_code, finish. Declared ONCE; the System page renders straight off this list. A tool that can be offloaded is routed to its HPC executor at build time.",
          "约 10 个常驻、带 JSON schema 的类型化工具：分析线、annotate_variants、map_phenotype_to_hpo、run_lirical、literature_search、deep_literature、scgpt_annotate、inspect_dataset、schematic、run_code、finish。只声明一次；系统页面直接由该列表渲染。可下发的工具在构建时被路由到对应的 HPC 执行器。")],
      [t("Skills — atomic & rewritable", "技能 —— 原子且可重写"), TEAL, "skills/*/SKILL.md",
        t("14 folder-shaped skills, each a SKILL.md (name + one-line description + body) plus a reference.py CodeAct demonstration the Scientist ADAPTS rather than executes verbatim. Loaded on demand through three-level progressive disclosure, and multi-selectable by the user in the console's Advanced panel.",
          "14 个以文件夹为形态的技能，每个包含 SKILL.md（名称 + 一行描述 + 正文）以及一个 reference.py 的 CodeAct 示范代码——科学家会「改编」它，而不是逐字执行。通过三级渐进式披露按需加载，用户也可在控制台高级面板中多选。")],
      [t("Preset pipelines — fixed workflows", "预置流水线 —— 固定工作流"), AMBER, "preset_pipelines/*/",
        t("7 researcher-auditable protocols that STEER the PI's planning: a PROTOCOL.md / SKILL.md with an at-a-glance step table, the agent-chosen vs auto-detected vs fixed-infra parameter split, and verified benchmark numbers. The PI can auto-select one; the user can pin one as mandatory.",
          "7 份研究者可审计的协议，用于「引导」首席研究员的规划：PROTOCOL.md / SKILL.md 提供一览式步骤表、参数三分法（智能体决定／自动探测／基础设施固定），以及经过验证的基准数字。首席研究员可自动选择其一；用户也可固定某个协议为必选。")],
    ];
    let y = 1.42;
    layers.forEach(([h, col, path, b], i) => {
      panel(s, M, y, CW, 1.5, i % 2 === 0 ? TINT : WHITE, "DDDAE8");
      numDot(s, M + 0.24, y + 0.22, i + 1, col, 0.36);
      s.addText(h, { x: M + 0.74, y: y + 0.18, w: 5.2, h: 0.42, fontFace: BODY, fontSize: 14, bold: true, color: INK, margin: 0, valign: "middle" });
      s.addText(path, { x: M + 6.0, y: y + 0.2, w: 3.0, h: 0.38, fontFace: "Courier New", fontSize: 10, color: col, margin: 0, valign: "middle" });
      s.addText(b, { x: M + 0.74, y: y + 0.62, w: CW - 1.1, h: 0.86, fontFace: BODY, fontSize: 10.5, color: MUTED, margin: 0, valign: "top", lineSpacing: 13.5 });
      y += 1.62;
    });

    s.addText(t("Why the registry still earns its place: keeping ~10 tools always in the tool list is cheap, while 14 skill bodies as permanent context would not be. Skills are surfaced as a short manifest and fetched only when relevant — that is the whole context argument for the split.",
      "注册表为何仍然必要：让约 10 个工具常驻工具列表代价很低，而把 14 份技能正文常驻上下文则不然。技能只以短清单出现、按需取回——这正是做此拆分的上下文层面的全部理由。"), {
      x: M, y: 6.34, w: CW, h: 0.55, fontFace: BODY, fontSize: 10, italic: true, color: MUTED, margin: 0,
    });
    footer(s, FOOT, nextPage());
  }

  // ============================================================ 16 PIPELINES
  {
    const s = pres.addSlide();
    titleBar(s, t("What the system can complete today", "系统当前能完成的业务"),
      t("Seven preset pipelines — the business capability, stated as questions a researcher can actually ask", "七条预置流水线——以研究者真正会问的问题来表述业务能力"));

    const rows = [
      headRow([t("Pipeline", "流水线"), t("Business question it completes", "所完成的业务问题"), t("Input", "输入"), t("Engine · host", "引擎 · 运行位置")]),
      ...zebra([
        ["celltype_annotation", t("What cell types are in this dataset, and what defines each cluster?", "这个数据集里有哪些细胞类型？每个簇由什么定义？"), ".h5ad", t("scanpy · HPC3 job", "scanpy · HPC3 作业")],
        ["scgpt_annotation", t("Transfer reference cell-type labels to every cell with calibrated confidence — or independently cross-check labels the data already carries", "以带置信度的方式把参考细胞类型标签迁移到每个细胞——或独立复核数据自带的标签"), ".h5ad", t("scGPT · GPU batch job", "scGPT · GPU 批作业")],
        ["differential_expression", t("How does an experimental condition differ from control, per cell type, and what pathways does that implicate?", "实验条件相对对照在各细胞类型上有何差异？涉及哪些通路？"), ".h5ad", t("scanpy + gseapy · HPC3", "scanpy + gseapy · HPC3")],
        ["gene_signature_scoring", t("How strongly does each cell express this signature, and does it differ across clusters or conditions?", "每个细胞对该基因签名的表达强度如何？在簇或条件间是否存在差异？"), ".h5ad", t("scanpy · HPC3", "scanpy · HPC3")],
        ["perturbation_analysis", t("In a pooled CRISPR screen, which perturbations actually changed the transcriptome — and how?", "在混池 CRISPR 筛选中，哪些扰动真正改变了转录组？如何改变？"), ".h5ad", t("scanpy + E-distance · HPC3", "scanpy + E-距离 · HPC3")],
        [{ text: "variant_annotation", options: { bold: true, color: IND } }, t("Which variants in this VCF matter — gene, consequence, ClinVar significance, population rarity, predicted damage — and which rank first?", "这份 VCF 里哪些变异重要——基因、功能后果、ClinVar 意义、群体稀有度、有害性预测——以及谁排在最前？"), ".vcf / .vcf.gz", t("bcftools + offline VEP · HPC3", "bcftools + 离线 VEP · HPC3")],
        [{ text: "phenotype_variant_diagnosis", options: { bold: true, color: IND } }, t("Which disease does this patient have, and which variant explains it? Reconciles a variant shortlist with an HPO-driven per-disease differential", "这位患者得的是什么病？哪个变异能解释它？将变异短名单与 HPO 驱动的按病种鉴别诊断相互印证"), t("VCF + free-text case note", "VCF + 自由文本病例记录"), t("VEP + LIRICAL/Exomiser · HPC3", "VEP + LIRICAL/Exomiser · HPC3")],
      ], true),
    ];
    s.addTable(rows, tableOpts([2.35, 5.55, 1.85, 2.38], { y: 1.4, rowH: 0.58, fontSize: 9.2 }));

    s.addText(t("Every pipeline ends the same way: a deterministic, no-AI assembly of the exact figures and tables the tools produced, into a citable PDF/DOCX manuscript with a formatted references section built from accepted literature citations.",
      "每条流水线的结尾都是相同的：把工具实际产出的图与表，以确定性、无 AI 参与的方式装配成可引用的 PDF/DOCX 稿件，并附上由已采纳文献引用生成的规范参考文献部分。"), {
      x: M, y: 6.34, w: CW, h: 0.5, fontFace: BODY, fontSize: 10, italic: true, color: MUTED, margin: 0, valign: "top", lineSpacing: 13,
    });
    footer(s, FOOT, nextPage());
  }

  // ============================================================ 17 SECTION 05
  sectionSlide(pres, "05", t("Engineering choices", "工程与技术选型"),
    t("Which packages, why them, and what the alternatives cost", "选了哪些包、为什么，以及备选方案的代价"), nextPage());

  // ============================================================ 18 PACKAGES
  {
    const s = pres.addSlide();
    titleBar(s, t("Package selection and the reason for each", "包选型及其理由"),
      t("Only h5py is a hard dependency — everything else is an optional extra, which is what keeps CI light and offline", "唯一的硬依赖是 h5py——其余全部是可选 extra，这正是 CI 得以轻量且离线的原因"));

    const rows = [
      headRow([t("Layer", "层"), t("Chosen", "选型"), t("Why this and not the obvious alternative", "为什么是它，而不是那个显而易见的替代")]),
      ...zebra([
        [t("Web / streaming", "Web 与流式"), "FastAPI · uvicorn · WebSocket", t("Token-by-token role-tagged streaming with a per-run event log and late-subscriber replay; a request/response framework could not carry a multi-hour run.", "需要按角色标记的逐 token 流式、按运行的事件日志与迟到订阅者重放；纯请求／响应框架撑不起长达数小时的运行。")],
        [t("SSH transport", "SSH 传输"), "paramiko", t("Duo needs a programmatic keyboard-interactive callback and we need in-process port forwarding — shelling out to the ssh binary gives neither.", "Duo 需要可编程的 keyboard-interactive 回调，我们还需要进程内端口转发——直接调用 ssh 二进制两者都做不到。")],
        [t("Accounts", "账户"), "SQLAlchemy 2.0 · Alembic · psycopg3 · bcrypt", t("SQLite in dev/CI and Postgres in prod behind the same ORM; metadata in the DB, blobs on disk. No external identity service — nothing leaves campus.", "开发／CI 用 SQLite、生产用 Postgres，共享同一 ORM；元数据入库、大文件留盘。不依赖外部身份服务——数据不出校园。")],
        [t("Inference", "推理"), "vLLM · Qwen3.6-35B-A3B-AWQ", t("Open weights on the lab's own GPU, OpenAI-compatible /v1 so any client works, AWQ so it fits the card, continuous batching so a whole expert team runs on ONE GPU.", "开源权重跑在实验室自有 GPU 上；OpenAI 兼容 /v1 让任何客户端可用；AWQ 量化以适配显卡；连续批处理让一整个专家团队跑在同一张 GPU 上。")],
        [t("Single-cell", "单细胞"), "scanpy · anndata · gseapy · leidenalg", t("Pure-Python parity with the R stack (Seurat→scanpy, fgsea→gseapy, DESeq2→PyDESeq2); ADR-0001 keeps venv+pip over conda because HPC3 needs no root for it and CI stays fast.", "纯 Python 与 R 栈功能对等（Seurat→scanpy、fgsea→gseapy、DESeq2→PyDESeq2）；ADR-0001 决定保留 venv+pip 而非 conda，因为在 HPC3 上无需 root 且 CI 更快。")],
        [t("Literature", "文献"), "paper-qa[local] · Europe PMC", t("PaperQA2 grounded on the session's local Qwen with LOCAL sentence-transformer embeddings, so chunk text never leaves UCI; external queries are sanitized to a filename and the question.", "PaperQA2 以本会话的本地 Qwen 为生成模型，配本地 sentence-transformer 向量化，chunk 文本不出 UCI；对外查询被净化为文件名与问题本身。")],
        [t("Variants", "变异"), "bcftools · Ensembl VEP (offline) · ClinVar · CADD/REVEL/AlphaMissense · OpenSpliceAI", t("A C tool streams the VCF so Python never materializes it; the offline cache removes the REST rate limit entirely; plugins add predictor columns without new code.", "用 C 工具流式处理 VCF，Python 永不整体加载；离线缓存彻底消除 REST 限流；插件无需新增代码即可补齐预测器列。")],
        [t("Phenotype", "表型"), "LIRICAL v2.4.1 · Exomiser · HPO", t("A calibrated likelihood-ratio model over curated HPO/OMIM annotations — the confidence number comes from LIRICAL, never from the LLM.", "基于已策展 HPO/OMIM 注释的校准似然比模型——置信度数字来自 LIRICAL，绝不来自大模型。")],
        [t("Rendering", "渲染"), "pandoc · XeLaTeX · graphviz", t("Deterministic assembly of a manuscript from real artifacts. A model writes prose, never the figure or the table it cites.", "由真实产物确定性装配稿件。模型只写正文，绝不生成它所引用的图或表。")],
        [t("Containment", "容器"), "Singularity", t("The only container runtime HPC3 permits; no fakeroot on the cluster, so images are built remotely and staged read-only.", "HPC3 唯一允许的容器运行时；集群上没有 fakeroot，因此镜像远程构建后以只读方式投放。")],
      ], true),
    ];
    s.addTable(rows, tableOpts([1.5, 3.05, 7.58], { y: 1.45, rowH: 0.44, fontSize: 8.8 }));
    footer(s, FOOT, nextPage());
  }

  // ============================================================ 19 PRINCIPLES
  {
    const s = pres.addSlide();
    titleBar(s, t("The five selection principles behind those choices", "支撑上述选型的五条原则"),
      t("Each one is enforced by the test suite, not by convention", "每一条都由测试套件强制，而不是靠约定"));

    const pr = [
      [t("Inject, don't import", "注入，而非导入"), TEAL,
        t("chat_fn, count_tokens_fn, summarize_fn, the executor Protocol and the LLM client are all INJECTED. chat_context imports nothing from the gateway; quick_chat imports without paramiko. The whole agent loop therefore runs offline against scripted tool calls.",
          "chat_fn、count_tokens_fn、summarize_fn、执行器 Protocol 与大模型客户端一律「注入」。chat_context 不从网关导入任何东西；quick_chat 不装 paramiko 也能导入。因此整个智能体循环可以在离线的脚本化工具调用下运行。")],
      [t("Lazy, optional heaviness", "重依赖延迟且可选"), IND,
        t("Every heavy runtime lives behind a pyproject extra and a lazy import. A host without it gets a clean status=\"dependency_missing\", not a crash — so the core and CI stay light while the server carries the full stack.",
          "每个重运行时都藏在 pyproject 的 extra 与懒加载导入之后。缺少它的主机得到干净的 status=\"dependency_missing\"，而不是崩溃——因此内核与 CI 保持轻量，而服务器承载完整栈。")],
      [t("Stream, never materialize", "流式处理，绝不整体加载"), AMBER,
        t("The rule that separates the toy path from the production path: bcftools filters the VCF, VEP writes JSONL, and Python only parses line-by-line — peak memory is bounded no matter how large the input is.",
          "这条规则区分了玩具路径与生产路径：bcftools 过滤 VCF、VEP 输出 JSONL，Python 只逐行解析——无论输入多大，峰值内存都是有界的。")],
      [t("One interface, two implementations", "一套接口，两种实现"), IND_L,
        t("RemoteExecutor is implemented by the real SSH session and by an in-process MockExecutor; every offload executor also carries an in-process fallback. The same code path is exercised live and in tests.",
          "RemoteExecutor 由真实 SSH 会话与进程内 MockExecutor 双实现；每个下发执行器还自带进程内回退。同一条代码路径既跑在生产也跑在测试。")],
      [t("Degrade loudly, never silently", "降级要响，绝不能静悄悄"), RED,
        t("A degraded step is recorded in the diagnostics report and the final manuscript renders clean; a truncated input announces itself. The failure we design against is not a crash — it is a plausible wrong answer.",
          "被降级的步骤会记入诊断报告，而最终稿件保持干净；被截断的输入会主动声明自己。我们真正防范的失败不是崩溃，而是「看起来合理的错误答案」。")],
    ];
    let y = 1.44;
    pr.forEach(([h, col, b], i) => {
      const hh = 0.95;
      panel(s, M, y, CW, hh, i % 2 === 0 ? TINT : WHITE, "DDDAE8");
      s.addShape("ellipse", { x: M + 0.24, y: y + 0.34, w: 0.32, h: 0.32, fill: { color: col }, line: { color: col, width: 1 } });
      s.addText(String(i + 1), { x: M + 0.24, y: y + 0.34, w: 0.32, h: 0.32, fontFace: BODY, fontSize: 11, bold: true, color: WHITE, align: "center", valign: "middle", margin: 0 });
      s.addText(h, { x: M + 0.68, y: y + 0.16, w: 3.1, h: 0.7, fontFace: BODY, fontSize: 12.5, bold: true, color: INK, margin: 0, valign: "middle" });
      s.addText(b, { x: M + 3.9, y: y + 0.12, w: CW - 4.2, h: 0.8, fontFace: BODY, fontSize: 9.8, color: MUTED, margin: 0, lineSpacing: 12.5, valign: "middle" });
      y += hh + 0.11;
    });
    footer(s, FOOT, nextPage());
  }

  // ============================================================ 20 POSITIONING
  {
    const s = pres.addSlide();
    titleBar(s, t("Positioning vs cloud frontier-model science agents", "与云端前沿模型科研智能体的定位对比"),
      t("Compared on architectural posture — data residency, compute reach, domain depth — not on model quality", "对比的是架构姿态——数据驻留、算力可达性、领域纵深——而非模型能力"));

    const rows = [
      headRow([t("Axis", "维度"), t("A cloud science-agent platform", "云端科研智能体平台"), t("AiScientist", "AiScientist")]),
      ...zebra([
        [t("Data residency", "数据驻留"), t("Patient VCFs, case notes and raw matrices must be uploaded to a third party", "患者 VCF、病例记录与原始矩阵必须上传给第三方"), t("Nothing leaves campus: model, embeddings and data all sit on UCI hardware; a guard inspects every prompt", "数据不出校园：模型、向量与数据全部位于 UCI 硬件；每条 prompt 都过守卫检查")],
        [t("Compute", "算力"), t("A managed sandbox sized for general code, billed per use", "面向通用代码的托管沙箱，按用量计费"), t("The lab's own Slurm allocation — WGS-scale jobs, multi-hour GPU serves, no per-token bill", "实验室自有 Slurm 配额——WGS 规模作业、数小时 GPU 服务、无按 token 计费")],
        [t("Reference data", "参考数据"), t("Cannot mount a 96 GB CADD or a 131 GB dbNSFP; licensed resources are out of reach", "无法挂载 96 GB 的 CADD 或 131 GB 的 dbNSFP；受限资源触达不到"), t("Reads the lab's already-staged references in place on the same filesystem the job runs on", "在作业运行的同一文件系统上，就地读取实验室已投放的参考数据")],
        [t("Domain depth", "领域纵深"), t("General code execution — the agent must reinvent the protocol each time", "通用代码执行——智能体每次都要重新发明协议"), t("Typed first-class tools for offline VEP, LIRICAL, scGPT and scanpy, plus the lab's own IRD protocol reproduced in code", "为离线 VEP、LIRICAL、scGPT、scanpy 提供一等公民的类型化工具，并在代码中复现了实验室自有的 IRD 协议")],
        [t("Auditability", "可审计性"), t("A transcript of what the agent did", "一份智能体行为的对话记录"), t("run_state.json, provenance stamps, a resumable run, a deterministic no-AI report, and a diagnostics record of every degradation", "run_state.json、溯源标记、可恢复的运行、确定性无 AI 报告，以及每次降级的诊断记录")],
        [t("Anti-fabrication", "反臆造"), t("Prompt-level instructions", "以 prompt 层面的指令为主"), t("Code-level: a deterministic guard blocks accepting a failed step, and post-generation verification corrects the manuscript regardless of the model", "落在代码层：确定性守卫禁止「接受」失败步骤，生成后校验无视模型内容直接纠正稿件")],
      ], true),
    ];
    s.addTable(rows, tableOpts([1.9, 4.65, 5.58], { y: 1.46, rowH: 0.6, fontSize: 9.2 }));

    panel(s, M, 5.96, CW, 0.86, TINT2);
    s.addText([
      { text: t("Where they are ahead — stated honestly:  ", "他们领先之处——如实陈述："), options: { bold: true, color: INK } },
      { text: t("frontier reasoning quality, breadth of general tooling, and managed uptime with no ops burden. Our answers are a pluggable LLM backend (any OpenAI-compatible endpoint) so the model is A/B-swappable, and a planned LangGraph + Postgres-checkpointer port of the loop.",
        "前沿推理质量、通用工具广度，以及零运维负担的托管可用性。我们的应对是：可插拔的大模型后端（任意 OpenAI 兼容端点），使模型可 A/B 替换；以及规划中的 LangGraph + Postgres checkpointer 循环重构。"), options: { color: MUTED } },
    ], { x: M + 0.24, y: 6.06, w: CW - 0.48, h: 0.68, fontFace: BODY, fontSize: 10, margin: 0, valign: "top", lineSpacing: 13 });
    footer(s, FOOT, nextPage());
  }

  // ============================================================ 21 SECTION 06
  sectionSlide(pres, "06", t("Genetic variant annotation", "基因变异注释"),
    t("The line under active development — architecture, parity target, verified numbers, open gaps",
      "当前在研的工作线——架构、对齐目标、已验证数据、待补缺口"), nextPage());

  // ============================================================ 22 WHY HARD
  {
    const s = pres.addSlide();
    titleBar(s, t("Two failure modes this line exists to fix", "这条线要修复的两种失败模式"),
      t("Both were found by running real lab data, not by review", "两者都是跑真实实验室数据发现的，不是评审看出来的"));

    const fails = [
      [t("Failure 1 — it did not SCALE", "失败一 —— 撑不起规模"), RED,
        t("The original REST path reads the whole VCF into a Python string, caps at 500 variants, and is rate-limited by the public Ensembl API. A 1.1 GB WGS VCF was silently annotated as its first 500 variants — and then reported as a \"500-variant cohort\". The truncation was invisible in the manuscript.",
          "最初的 REST 路径把整个 VCF 读入 Python 字符串，上限 500 个变异，还受公共 Ensembl API 限流。一个 1.1 GB 的 WGS VCF 被悄悄按前 500 个变异注释——然后被报告成「500 变异队列」。这个截断在稿件里完全看不出来。"),
        t("An offline line — bcftools streams, VEP reads a bind-mounted local cache with --fork, Python parses JSONL line-by-line. Bounded memory, no network, no cap.",
          "改为离线线——bcftools 流式过滤，VEP 以 --fork 读取绑定挂载的本地缓存，Python 逐行解析 JSONL。内存有界、无需网络、无上限。")],
      [t("Failure 2 — it was not CLINICAL", "失败二 —— 不够临床"), AMBER,
        t("Benchmarked against the lab's own IRD reference on the SAME VCF: annotation was sound (~94% gene concordance) but the shortlist was clinically off-target — mitochondrial, PRAMEF and lncRNA noise at the top, while RP1L1 and CRB1 were missed. A generic protocol is not an IRD protocol.",
          "在同一份 VCF 上与实验室自有的 IRD 参考流程对标：注释本身是可靠的（基因一致性约 94%），但候选短名单在临床上跑偏——线粒体、PRAMEF 与 lncRNA 噪声排在最前，而 RP1L1 与 CRB1 被漏掉。通用协议不等于 IRD 协议。"),
        t("Reproduce the lab's known-gene-first strategy in our own code — a 258-gene RetNet panel applied BEFORE annotation, a rarity floor, then tiering by inheritance model.",
          "在我们自己的代码中复现实验室「已知基因优先」的策略——在注释之前先应用 258 基因的 RetNet 面板，再加稀有度下限，最后按遗传模式分层。")],
    ];
    let x = M;
    fails.forEach(([h, col, body, fix]) => {
      const w = 5.98;
      panel(s, x, 1.44, w, 3.35, WHITE, "DDDAE8");
      s.addShape("roundRect", { x, y: 1.44, w, h: 0.5, fill: { color: col }, rectRadius: 0.07, line: { color: col, width: 1 } });
      s.addShape("rect", { x, y: 1.76, w, h: 0.18, fill: { color: col }, line: { color: col, width: 1 } });
      s.addText(h, { x: x + 0.22, y: 1.48, w: w - 0.44, h: 0.42, fontFace: BODY, fontSize: 13, bold: true, color: WHITE, margin: 0, valign: "middle" });
      s.addText(body, { x: x + 0.22, y: 2.08, w: w - 0.44, h: 1.5, fontFace: BODY, fontSize: 10.5, color: INK, margin: 0, valign: "top", lineSpacing: 14 });
      panel(s, x + 0.22, 3.58, w - 0.44, 1.04, TINT);
      s.addText([{ text: t("Fix  ", "修复  "), options: { bold: true, color: col } }, { text: fix, options: { color: MUTED } }],
        { x: x + 0.38, y: 3.66, w: w - 0.76, h: 0.9, fontFace: BODY, fontSize: 9.8, margin: 0, valign: "top", lineSpacing: 12.5 });
      x += w + 0.17;
    });

    panel(s, M, 5.0, CW, 1.55, TINT2);
    s.addText(t("Four engineering hazards found and fixed on the way", "沿途发现并修复的四个工程隐患"), {
      x: M + 0.24, y: 5.1, w: 6.0, h: 0.32, fontFace: BODY, fontSize: 12, bold: true, color: INK, margin: 0,
    });
    const hz = [
      t("vep.sif ships /data as a symlink into the read-only VEP cache, so a $HOME scratch bind resolved into it and Singularity died at container creation (exit 127, ~1 s) — silently killing EVERY offline job. Scratch now lives on dfs3b.",
        "vep.sif 中 /data 是指向只读 VEP 缓存的符号链接，因此绑定到 $HOME 的临时目录会解析进该缓存，Singularity 在容器创建阶段直接失败（exit 127，约 1 秒）——悄无声息地杀死了「每一个」离线作业。现在临时目录改到 dfs3b。"),
      t("Assembly was not auto-detected; an hg19 VCF annotated as GRCh38 gives roughly 90% wrong genes. It is now read from the VCF header (chr1 contig length), with GRCh37 as the fallback — most eye/IRD data is GRCh37.",
        "此前不自动识别参考基因组版本；把 hg19 的 VCF 当作 GRCh38 注释，约 90% 的基因会错。现在从 VCF 头部（chr1 contig 长度）读取，并以 GRCh37 为兜底——多数眼科／IRD 数据是 GRCh37。"),
      t("ClinVar's .tbi index was not bind-mounted alongside its .vcf.gz, so VEP's --custom Tabix lookup silently produced nothing.",
        "ClinVar 的 .tbi 索引没有与 .vcf.gz 一起绑定挂载，导致 VEP --custom 的 Tabix 查询悄悄查不到任何东西。"),
      t("Without a re-run guard the model re-called annotate_variants in a later step — a fresh ~45-minute WGS VEP job that OVERWROTE good tables. Opt-in per-run memoization now blocks that.",
        "没有重跑守卫时，模型会在后续步骤里再次调用 annotate_variants——那是一个全新的约 45 分钟 WGS VEP 作业，并会「覆盖」已有的好结果表。现在通过可选的按运行记忆化阻止了这一点。"),
    ];
    let hx = M + 0.24;
    hz.forEach((z, i) => {
      const w = 2.85;
      numDot(s, hx, 5.48, i + 1, IND, 0.26);
      s.addText(z, { x: hx, y: 5.8, w, h: 0.72, fontFace: BODY, fontSize: 8.5, color: MUTED, margin: 0, lineSpacing: 11 });
      hx += w + 0.11;
    });
    footer(s, FOOT, nextPage());
  }

  // ============================================================ 23 PIPELINE
  {
    const s = pres.addSlide();
    titleBar(s, t("The offline variant pipeline", "离线变异注释流水线"),
      t("Runs inside vep.sif on an HPC3 CPU node, driven by variant_cli + the Slurm analysis executor", "在 HPC3 CPU 节点的 vep.sif 内运行，由 variant_cli + Slurm 分析执行器驱动"));

    const steps = [
      ["QC", t("Ti/Tv, Het/Hom, call rate; flags values outside WGS/WES ranges", "Ti/Tv、杂合/纯合比、call rate；标记超出 WGS/WES 预期范围的数值")],
      [t("Filter + normalise", "过滤 + 归一化"), t("bcftools PASS-filter streams the VCF; an optional left-align / split-multiallelic norm when a reference FASTA is staged", "bcftools 流式做 PASS 过滤；若已投放参考 FASTA，则可选做左对齐／多等位拆分")],
      [t("Panel BEFORE VEP", "先面板，后 VEP"), t("restrict to the disease-gene panel as regions_bed — exons AND introns — plus a population-AF rarity floor", "以 regions_bed 限定到疾病基因面板——外显子与内含子皆包含——并加上群体等位基因频率的稀有度下限")],
      ["VEP --offline", t("gene, consequence, canonical/MANE, SIFT/PolyPhen, gnomAD AF; ClinVar via --custom; CADD / REVEL / AlphaMissense via plugins", "基因、功能后果、canonical/MANE、SIFT/PolyPhen、gnomAD 频率；ClinVar 经 --custom 引入；CADD / REVEL / AlphaMissense 经插件引入")],
      [t("Splice + IRD layers", "剪接 + IRD 层"), t("OpenSpliceAI inside the same container (gated); HGMD, retina-specific exons, retina ATAC peaks, dbscSNV", "同一容器内的 OpenSpliceAI（开关控制）；HGMD、视网膜特异外显子、视网膜 ATAC 峰、dbscSNV")],
      [t("Tier + deliver", "分层 + 交付"), t("per-gene inheritance model — dominant ≤1e-4 · recessive ≥2 alleles ≤5e-3 · X-linked — then 5 standard tables and a ranked shortlist", "按基因判定遗传模式——显性 ≤1e-4 · 隐性 ≥2 个等位 ≤5e-3 · X 连锁——随后输出 5 张标准表与排序后的短名单")],
    ];
    let x = M;
    steps.forEach(([h, b], i) => {
      const w = 1.9;
      panel(s, x, 1.44, w, 2.75, i < 3 ? TINT : WHITE, "DDDAE8");
      numDot(s, x + 0.13, 1.58, i + 1, i < 3 ? TEAL : IND, 0.3);
      s.addText(h, { x: x + 0.1, y: 1.94, w: w - 0.2, h: 0.5, fontFace: BODY, fontSize: 11, bold: true, color: INK, margin: 0 });
      s.addText(b, { x: x + 0.1, y: 2.44, w: w - 0.2, h: 1.66, fontFace: BODY, fontSize: 8.8, color: MUTED, margin: 0, lineSpacing: 11.5 });
      if (i < 5) arrow(s, x + w + 0.01, 2.8, 0.16);
      x += w + 0.14;
    });

    // verified numbers
    panel(s, M, 4.38, 7.4, 2.2, DARK);
    s.addText(t("Verified end-to-end on HPC3 (2026-07-13, same benchmark VCF)", "在 HPC3 上端到端验证（2026-07-13，同一基准 VCF）"), {
      x: M + 0.26, y: 4.5, w: 6.9, h: 0.34, fontFace: BODY, fontSize: 12, bold: true, color: TEAL, margin: 0,
    });
    const nums = [
      ["~52 min → 99 s", t("whole-VCF annotation, after restricting to the panel before VEP", "在 VEP 之前限定面板后，全 VCF 注释耗时")],
      ["4.67 M → 1,544", t("variants VEP actually has to see", "VEP 实际需要处理的变异数")],
      ["CRB1 · TRPM1 · CROCC", t("the shortlist now leads with real retinal-disease genes", "短名单现在由真正的视网膜疾病基因领衔")],
    ];
    let ny = 4.92;
    nums.forEach(([n, l]) => {
      s.addText(n, { x: M + 0.26, y: ny, w: 2.9, h: 0.36, fontFace: BODY, fontSize: 14, bold: true, color: WHITE, margin: 0, valign: "middle" });
      s.addText(l, { x: M + 3.3, y: ny, w: 3.85, h: 0.4, fontFace: BODY, fontSize: 9.5, color: "B9B4D6", margin: 0, valign: "middle" });
      ny += 0.5;
    });

    panel(s, M + 7.58, 4.38, 4.55, 2.2, TINT2);
    s.addText(t("The standout finding", "最值得注意的发现"), {
      x: M + 7.82, y: 4.5, w: 4.1, h: 0.32, fontFace: BODY, fontSize: 12, bold: true, color: INK, margin: 0,
    });
    s.addText(t("ClinVar pathogenic / likely-pathogenic hits in CRB1 and USH2A — and a compound-heterozygous CRB1 candidate in the same patient: one ClinVar-pathogenic allele plus a novel loss-of-function allele. This is the lab-style IRD output the earlier generic run could not produce.",
      "在 CRB1 与 USH2A 上命中 ClinVar 致病／可能致病变异——并在同一患者身上发现复合杂合的 CRB1 候选：一个 ClinVar 致病等位加上一个新发的功能缺失等位。这正是此前的通用流程产不出来的、实验室风格的 IRD 结果。"), {
      x: M + 7.82, y: 4.86, w: 4.1, h: 1.6, fontFace: BODY, fontSize: 9.5, color: MUTED, margin: 0, valign: "top", lineSpacing: 12.5,
    });
    footer(s, FOOT, nextPage());
  }

  // ============================================================ 24 PARITY MATRIX
  {
    const s = pres.addSlide();
    titleBar(s, t("IRD parity: 11 layers, honestly scored", "IRD 对齐：11 个层次，如实评分"),
      t("The target is clinical-grade parity — the same candidates surface — not byte-identical output; engines differ (our VEP/Ensembl vs their ANNOVAR/refGene)",
        "目标是「临床级对齐」——同样的候选能浮出来——而不是逐字节一致；引擎本身就不同（我们用 VEP/Ensembl，他们用 ANNOVAR/refGene）"));

    const DONE = { text: LANG === "en" ? "DONE" : "已完成", options: { color: "0E8577", bold: true } };
    const WIRE = { text: LANG === "en" ? "GATED" : "已建待开", options: { color: AMBER, bold: true } };
    const TODO = { text: LANG === "en" ? "TO BUILD" : "待建设", options: { color: RED, bold: true } };

    const rows = [
      headRow(["#", t("IRD layer", "IRD 层"), t("Our state", "我方现状"), t("Status", "状态"), t("Phase", "阶段")]),
      ...zebra([
        ["1", t("Known-gene / panel first", "已知基因／面板优先"), t("258-gene RetNet panel imported as a repo asset and injected as the default gene set", "258 基因的 RetNet 面板已作为仓库资产引入，并作为默认基因集注入"), DONE, "1"],
        ["2", t("Disease-model allele frequency", "按疾病模式的等位基因频率"), t("dominant ≤1e-4 / recessive ≥2 alleles ≤5e-3 / X-linked, replacing a single flat cutoff", "显性 ≤1e-4／隐性 ≥2 个等位 ≤5e-3／X 连锁，取代单一平坦阈值"), DONE, "2"],
        ["3", t("Protein-altering focus", "蛋白改变优先"), t("consequence + impact present; promoted as an explicit shortlist tier", "功能后果与影响等级已具备；作为显式的短名单分层被提升"), DONE, "2"],
        ["4", t("Known-mutation match (HGMD)", "已知致病突变匹配（HGMD）"), t("annotator ported; the lab's public HGMD copy on HPC3 is authorized for reuse", "注释器已移植；实验室在 HPC3 上的 HGMD 公开版本已获授权复用"), WIRE, "3"],
        ["5", t("Retina-specific exons", "视网膜特异外显子"), t("BED overlap annotator ported; the Eric Pierce RNA-seq BED is on HPC3", "BED 重叠注释器已移植；Eric Pierce 的 RNA-seq BED 已在 HPC3 上"), WIRE, "3"],
        ["6", t("Splice prediction", "剪接预测"), t("OpenSpliceAI built and verified (DS_DL 0.917 / 0.755); dbscSNV annotator ported", "OpenSpliceAI 已构建并验证（DS_DL 0.917 / 0.755）；dbscSNV 注释器已移植"), WIRE, "1/3"],
        ["7", t("Deep predictor panel", "深度预测器面板"), t("VEP plugins for CADD / REVEL / AlphaMissense coded; 96 GB CADD + 2.3 GB REVEL + 131 GB dbNSFP staged on HPC3", "CADD／REVEL／AlphaMissense 的 VEP 插件已编码；96 GB CADD + 2.3 GB REVEL + 131 GB dbNSFP 已在 HPC3 上就位"), WIRE, "1"],
        ["8", t("Gene-level constraint", "基因层面的约束度"), t("pLI / pRec / RVIS / GDI not yet annotated or folded into tiering", "pLI／pRec／RVIS／GDI 尚未注释，也未纳入分层"), TODO, "2"],
        ["9", t("Phenotype integration", "表型整合"), t("LIRICAL + Exomiser line built and verified; Exomiser gene-phenotype scoring not yet folded into the variant tiering", "LIRICAL + Exomiser 线已建成并验证；Exomiser 的基因-表型评分尚未纳入变异分层"), WIRE, "3"],
        ["10", t("Compound-het phasing", "复合杂合定相"), t("cis/trans phasing from genotypes not implemented — frequency + count gate only", "尚未实现基于基因型的 cis/trans 定相——目前仅有频率 + 计数门槛"), TODO, "2"],
        ["11", t("Regulatory accessibility", "调控可及性"), t("narrowPeak overlap annotator ported; the retina ATAC peak file is on HPC3", "narrowPeak 重叠注释器已移植；视网膜 ATAC 峰文件已在 HPC3 上"), WIRE, "3"],
      ], true),
    ];
    s.addTable(rows, tableOpts([0.4, 2.55, 6.5, 1.28, 0.7], { y: 1.6, rowH: 0.38, fontSize: 8.6 }));

    s.addText(t("GATED = the code exists and the data is staged; what remains is an ops decision (an env flag, a deploy) rather than engineering.",
      "「已建待开」= 代码已存在、数据已就位，剩下的是运维决策（一个环境开关、一次部署），而非工程开发。"), {
      x: M, y: 6.5, w: CW, h: 0.4, fontFace: BODY, fontSize: 9.5, italic: true, color: MUTED, margin: 0,
    });
    footer(s, FOOT, nextPage());
  }

  // ============================================================ 25 PHENOTYPE
  {
    const s = pres.addSlide();
    titleBar(s, t("The phenotype track — and where it structurally stops", "表型轨道 —— 以及它在结构上的边界"),
      t("A variant shortlist cannot say WHICH disease; a phenotype cannot say WHICH variant. Run both, then reconcile.",
        "变异短名单说不出「是哪个病」；表型说不出「是哪个变异」。因此两条轨道独立运行，再相互印证。"));

    const track = [
      [t("Free text → HPO", "自由文本 → HPO"), TEAL,
        t("The clinician writes in their own words, in any language. An LLM extracts phrases and negation; the ONTOLOGY owns the identifiers — the model picks a candidate NUMBER and never types an HPO ID. The lexicon is committed to the repo, so it runs offline.",
          "临床医生用自己的语言书写，任何语种皆可。大模型负责抽取短语与否定；「标识符归本体所有」——模型只挑选候选项的编号，绝不自己敲出 HPO ID。词表已随仓库提交，因此可离线运行。")],
      [t("HPO + VCF → LIRICAL", "HPO + VCF → LIRICAL"), IND,
        t("LIRICAL v2.4.1 with Exomiser fuses the HPO profile with the variant shortlist into a per-disease POST-TEST PROBABILITY via a likelihood-ratio model. Rare, specific symptoms carry more information than common ones — which is exactly what handles IRD's heavy symptom overlap.",
          "LIRICAL v2.4.1 配合 Exomiser，用似然比模型把 HPO 表型谱与变异短名单融合为「按病种的后验概率」。罕见而特异的症状携带更多信息——这正是应对 IRD 症状高度重叠的关键。")],
      [t("Two numbers, never averaged", "两个数字，绝不平均"), AMBER,
        t("LIRICAL's calibrated probability is the trusted confidence. The literature track returns a ClinGen-style gene-disease VALIDITY tier — an evidence GRADE, not a probability — so it can only ATTACH support to a call or RESCUE a candidate for human review, never be blended into the number.",
          "LIRICAL 的校准概率是可信的置信度。文献轨道返回 ClinGen 风格的基因-疾病「效力等级」——那是证据「等级」而非概率——因此它只能为某个判定「附加支持」，或把某个候选「捞回」交人工复核，绝不会被混入那个数字。")],
    ];
    let x = M;
    track.forEach(([h, col, b], i) => {
      const w = 3.87;
      panel(s, x, 1.5, w, 2.5, WHITE, "DDDAE8");
      s.addShape("roundRect", { x, y: 1.5, w, h: 0.48, fill: { color: col }, rectRadius: 0.07, line: { color: col, width: 1 } });
      s.addShape("rect", { x, y: 1.8, w, h: 0.18, fill: { color: col }, line: { color: col, width: 1 } });
      s.addText(h, { x: x + 0.2, y: 1.53, w: w - 0.4, h: 0.42, fontFace: BODY, fontSize: 12, bold: true, color: WHITE, margin: 0, valign: "middle" });
      s.addText(b, { x: x + 0.2, y: 2.1, w: w - 0.4, h: 1.78, fontFace: BODY, fontSize: 9.8, color: INK, margin: 0, valign: "top", lineSpacing: 12.5 });
      if (i < 2) arrow(s, x + w + 0.005, 2.75, 0.145);
      x += w + 0.16;
    });

    panel(s, M, 4.18, 5.98, 2.42, TINT);
    s.addText(t("Verified on the demo case", "在演示病例上的验证"), { x: M + 0.24, y: 4.28, w: 5.4, h: 0.32, fontFace: BODY, fontSize: 12, bold: true, color: INK, margin: 0 });
    const ver = [
      t("An informal note mapped to 4 observed + 12 excluded HPO terms with no human curation", "一份非正式的病例记录，在无人工策展的情况下映射出 4 个观察到 + 12 个排除的 HPO 术语"),
      t("LIRICAL ranked the true answer, IMPG2, at #1 (compositeLR 12.647) — reproduced on both a synthetic and a real WGS VCF", "LIRICAL 把正确答案 IMPG2 排在第 1 位（compositeLR 12.647）——在合成 VCF 与真实 WGS VCF 上均可复现"),
      t("Pertinent negatives do real work: adding 5 \"the patient does NOT have…\" sentences drove the wrong front-runner from #1 to #18", "「阴性症状」确实起作用：补上 5 句「患者没有……」，使原本排第 1 的错误答案掉到第 18 位"),
    ];
    s.addText(ver.map((v, i) => ({ text: v, options: { bullet: true, breakLine: i < ver.length - 1, paraSpaceAfter: 7 } })), {
      x: M + 0.24, y: 4.66, w: 5.5, h: 1.8, fontFace: BODY, fontSize: 10, color: MUTED, margin: 0, valign: "top", lineSpacing: 13,
    });

    panel(s, M + 6.14, 4.18, 5.99, 2.42, "FBEEE9", "E8C8BC");
    s.addText(t("The structural limit — state it, don't hide it", "结构性局限——要讲清楚，不要藏着"), {
      x: M + 6.38, y: 4.28, w: 5.5, h: 0.32, fontFace: BODY, fontSize: 12, bold: true, color: RED, margin: 0,
    });
    s.addText(t("Evaluated on 20 real solved cases from the lab (7 VCFs run): the pipeline recovers the causal gene when that gene has a known OMIM disease — rank-1 twice, top-6 in 4 of 7. But it CANNOT rank a gene with zero OMIM entries, because both LIRICAL and the panel are built on curated gene-disease annotations. The lab's cases are enriched for exactly those novel genes (AP5B1, WASF3). Closing this needs a variant-FIRST line, not a better prompt.",
      "在实验室 20 个真实已解病例（其中 7 份 VCF 实跑）上评估：当致病基因具有已知 OMIM 疾病时，流水线能召回它——2 例排第 1，7 例中有 4 例进前 6。但它「无法」对 OMIM 条目为零的基因排序，因为 LIRICAL 与基因面板都建立在已策展的基因-疾病注释之上。而实验室的病例恰恰富集了这类新基因（AP5B1、WASF3）。补上这一块需要一条「变异优先」的线，而不是更好的 prompt。"), {
      x: M + 6.38, y: 4.66, w: 5.5, h: 1.8, fontFace: BODY, fontSize: 9.8, color: "7A3D33", margin: 0, valign: "top", lineSpacing: 13,
    });
    footer(s, FOOT, nextPage());
  }

  // ============================================================ 26 ROADMAP
  {
    const s = pres.addSlide();
    titleBar(s, t("Roadmap and current status", "路线图与当前状态"),
      t("Four phases, ordered by leverage-per-risk; the data blockers are cleared", "四个阶段，按「杠杆／风险比」排序；数据层面的阻塞已全部解除"));

    const phases = [
      [t("Phase 0", "阶段 0"), t("Deploy the already-fixed regressions", "部署已修复的回归问题"), "0E8577",
        t("An inline comment on a production .env value silently fell back to defaults — vLLM ran a 32K window instead of the configured 128K, and run_code fell off HPC3. Fixed in code; the remaining item is a duplicate key dedupe at the next .env edit.",
          "生产 .env 中某个值后面的行内注释会导致静默回落到默认值——vLLM 实际以 32K 窗口运行而非配置的 128K，run_code 也没跑在 HPC3 上。代码已修复；剩下的是下次编辑 .env 时清理重复键。")],
      [t("Phase 1", "阶段 1"), t("Turn on what is already built", "把已建成的东西打开"), IND,
        t("Highest leverage, lowest risk: the panel is wired, the predictors are staged, SpliceAI is built and verified. What remains is env flags and a benchmark re-run — no new logic.",
          "杠杆最高、风险最低：面板已接通、预测器已就位、SpliceAI 已构建并验证。剩下的只是环境开关与一次基准重跑——无需新逻辑。")],
      [t("Phase 2", "阶段 2"), t("Prioritization logic in code", "把优先级逻辑写进代码"), AMBER,
        t("Disease-model AF tiers and the protein-altering tier are done. Still to build: gene-level constraint (pLI/pRec/RVIS/GDI) annotation, and cis/trans compound-het phasing from genotypes.",
          "疾病模式的频率分层与蛋白改变分层已完成。仍待建设：基因层面约束度（pLI/pRec/RVIS/GDI）注释，以及基于基因型的 cis/trans 复合杂合定相。")],
      [t("Phase 3", "阶段 3"), t("External-data layers", "外部数据层"), IND_L,
        t("HGMD, retina-specific exons, retina ATAC and Exomiser scoring — all annotators are ported and all reference data is staged and authorized. ONE open item remains, and it is a product decision, not a file: how patient HPO terms enter a run.",
          "HGMD、视网膜特异外显子、视网膜 ATAC 与 Exomiser 评分——注释器已全部移植，参考数据已全部就位且获授权。仅剩一项待定，而且它是产品决策而非文件问题：患者 HPO 术语以何种方式进入一次运行。")],
    ];
    let y = 1.44;
    phases.forEach(([p, h, col, b]) => {
      panel(s, M, y, CW, 1.1, WHITE, "DDDAE8");
      s.addShape("roundRect", { x: M + 0.2, y: y + 0.2, w: 1.15, h: 0.42, fill: { color: col }, rectRadius: 0.06, line: { width: 0 } });
      s.addText(p, { x: M + 0.2, y: y + 0.2, w: 1.15, h: 0.42, fontFace: BODY, fontSize: 11, bold: true, color: WHITE, align: "center", valign: "middle", margin: 0 });
      s.addText(h, { x: M + 1.5, y: y + 0.16, w: 3.5, h: 0.5, fontFace: BODY, fontSize: 12.5, bold: true, color: INK, margin: 0, valign: "middle" });
      s.addText(b, { x: M + 5.1, y: y + 0.14, w: CW - 5.35, h: 0.94, fontFace: BODY, fontSize: 9.8, color: MUTED, margin: 0, lineSpacing: 12.5, valign: "middle" });
      y += 1.2;
    });

    panel(s, M, 6.22, CW, 0.64, TINT2);
    s.addText([
      { text: t("Beyond the variant line:  ", "变异线之外："), options: { bold: true, color: INK } },
      { text: t("a LangGraph + Postgres-checkpointer port of the loop, provenance stamping toward Kosmos-parity, multi-cycle research loops, and a pluggable backend so a lab can bring its own endpoint in place of HPC3.",
        "把主循环移植到 LangGraph + Postgres checkpointer、朝 Kosmos 对齐的溯源标记、多轮次研究循环，以及可插拔后端——让实验室可以用自己的端点替代 HPC3。"), options: { color: MUTED } },
    ], { x: M + 0.24, y: 6.28, w: CW - 0.48, h: 0.52, fontFace: BODY, fontSize: 10, margin: 0, valign: "middle" });
    footer(s, FOOT, nextPage());
  }

  // ============================================================ 27 CLOSING
  {
    const s = pres.addSlide();
    bg(s, DARK);
    s.addShape("ellipse", { x: 10.2, y: -1.6, w: 5.0, h: 5.0, fill: { color: "201C40" }, line: { width: 0 } });
    s.addText(t("In one sentence", "一句话总结"), {
      x: M, y: 1.15, w: 9.0, h: 0.5, fontFace: BODY, fontSize: 13, color: TEAL, margin: 0,
    });
    s.addText(t("Real compute, real tools, real data — kept on the lab's own hardware, with the honesty guarantees written in code rather than in prompts.",
      "真实的算力、真实的工具、真实的数据——全部留在实验室自有硬件上，而「诚实」的保证写在代码里，而不是写在 prompt 里。"), {
      x: M, y: 1.65, w: 11.0, h: 1.2, fontFace: HEAD, fontSize: 27, bold: true, color: WHITE, margin: 0, lineSpacing: 36,
    });

    const takeaways = [
      [t("State", "状态"), t("Layered by lifetime: session, run, step, durable record — so a run is isolatable, cancellable and resumable.", "按生命周期分层：会话、运行、步骤、持久记录——因此一次运行可隔离、可取消、可恢复。")],
      [t("Context", "上下文"), t("Two paths with one estimator, exact server-side counting, and compaction that degrades rather than fails.", "两条路径共用一套估算器、服务端精确计数，压缩失败时降级而不是报错。")],
      [t("Compute", "算力"), t("One RemoteExecutor interface over SSH; Slurm owns the job, an atomic registry lets us reattach after a restart.", "SSH 之上的单一 RemoteExecutor 接口；作业归 Slurm 所有，原子注册表让我们在重启后重新挂接。")],
      [t("Variants", "变异"), t("WGS-scale offline annotation with the lab's IRD protocol reproduced — panel-first turned 52 minutes into 99 seconds.", "WGS 规模的离线注释，并复现了实验室的 IRD 协议——面板优先把 52 分钟变成 99 秒。")],
    ];
    let x = M;
    takeaways.forEach(([h, b], i) => {
      const w = 2.86;
      s.addShape("roundRect", { x, y: 3.4, w, h: 1.9, fill: { color: "221E45" }, rectRadius: 0.07, line: { color: "332D5E", width: 1 } });
      s.addText(h, { x: x + 0.2, y: 3.55, w: w - 0.4, h: 0.36, fontFace: BODY, fontSize: 12.5, bold: true, color: [TEAL, IND_L, AMBER, WHITE][i], margin: 0 });
      s.addText(b, { x: x + 0.2, y: 3.94, w: w - 0.4, h: 1.24, fontFace: BODY, fontSize: 9.8, color: "BAB5D8", margin: 0, valign: "top", lineSpacing: 12.5 });
      x += w + 0.23;
    });

    s.addText(t("AiScientist · UCI Vision / Ocular-Biology Lab · v0.2.0 \"DAG\"", "AiScientist · UCI 视觉／眼科生物学实验室 · v0.2.0 「DAG」"), {
      x: M, y: 6.4, w: 11.0, h: 0.34, fontFace: BODY, fontSize: 10.5, color: "7D78A3", margin: 0,
    });
    nextPage();
  }

  return pres.writeFile({ fileName: outFile });
}

(async () => {
  await buildDeck("en", process.argv[2] || "AiScientist_Technical_Spec_EN.pptx");
  await buildDeck("zh", process.argv[3] || "AiScientist_技术规格说明_中文.pptx");
  console.log("done");
})();
