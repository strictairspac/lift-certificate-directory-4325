import { api } from "../api/workbench.js";
import { errorText, field } from "../components/common/fields.js";
import { empty, pageHeading, section } from "../components/common/panel.js";
import { table } from "../components/common/table.js";
import { submit } from "../hooks/submit.js";
import { navigate } from "../router/router.js";
import { el } from "../utils/dom.js";
import { cell, time } from "../utils/format.js";

const today = () => new Date().toISOString().slice(0, 10);

function certTable(dossier) {
  const head = ["编号", "电梯", "失效日", "撤销"];
  const rows = dossier.certificates.map(c => [
    c.code, c.lift, c.expires_on, c.revoked === "1",
  ]);
  return el("div", {},
    table(head, rows, `${dossier.title}在册证书`),
    el("p", {class: "capacity-line"},
      `在册 ${dossier.certificates.length} 件 / 登记上限 ${dossier.capacity} 件`));
}

function swapForm(config, dossiers) {
  if (dossiers.length < 2) return empty("至少需要两份卷宗才能互换证书。");
  const [left, right] = dossiers;
  const issue = errorText();
  const form = el("form", {class: "transfer-form"},
    field("调出卷宗（甲）", selectDossier("left_dossier_id", dossiers, left.id)),
    codePicker("甲卷调出编号", "left_codes"),
    field("调入卷宗（乙）", selectDossier("right_dossier_id", dossiers, right.id)),
    codePicker("乙卷调出编号", "right_codes"),
    el("button", {type: "submit"}, "确认调拨"), issue);
  submit(form, async values => {
    const payload = {
      left_dossier_id: Number(values.get("left_dossier_id")),
      right_dossier_id: Number(values.get("right_dossier_id")),
      left_codes: splitCodes(values.get("left_codes")),
      right_codes: splitCodes(values.get("right_codes")),
    };
    const order = await api.createTransfer(payload);
    // 同单重试后端按幂等返回原单号；跳转后即按新归属出检证结论。
    navigate(`transfers/${order.id}`);
  });
  return form;
}

function selectDossier(name, dossiers, selected) {
  return el("select", {name},
    dossiers.map(d => el("option", {value: d.id, selected: d.id === selected},
      `${d.code} · ${d.title}`)));
}

function splitCodes(text) {
  return text.split(/[\s,，;；]+/).map(s => s.trim()).filter(Boolean);
}

function codePicker(label, name) {
  const textarea = el("textarea", {name, rows: 3, required: true, spellcheck: "false",
    placeholder: "每行一个编号，如 YT-Q01"});
  return field(label, textarea, "两侧数量必须一致；同一编号在本侧只能出现一次。");
}

function pointCheckPanel() {
  const issue = errorText();
  const output = el("div", {class: "point-check-output"});
  const form = el("form", {class: "point-check-form"},
    field("检证编号", el("textarea", {name: "probes", rows: 2, required: true,
      placeholder: "每行一个编号"})),
    field("巡检日", el("input", {name: "review_date", type: "date", required: true, value: today()})),
    el("button", {type: "submit"}, "按当前归属点查"), issue);
  submit(form, async values => {
    output.replaceChildren();
    const rows = await api.pointCheck({
      probes: splitCodes(values.get("probes")), review_date: values.get("review_date"),
    });
    output.append(table(["类别", "项目", "归属卷宗", "电梯或槽位", "桶线索", "结论"],
      rows.map(r => [r.类别, r.项目, r.归属卷宗, r.电梯或槽位, r.桶线索, r.结论]), "调拨点查结果"));
  });
  return el("div", {}, form, output);
}

function ordersTable(orders) {
  return table(["单号", "状态", "数量", "发起人", "时间", "操作"], orders.map(o => [
    o.id, o.status, o.quantity, o.actor, time(o.created_at),
    el("a", {href: `#transfers/${o.id}`}, "检证结果"),
  ]), "调拨单");
}

async function orderVerification(orderId, reviewDate, mount) {
  const data = await api.verifyTransfer(orderId, reviewDate);
  mount.replaceChildren(
    el("dl", {class: "metrics"},
      el("div", {}, el("dt", {}, "调拨数量"), el("dd", {}, cell(data.quantity))),
      el("div", {}, el("dt", {}, "单据状态"), el("dd", {}, data.status)),
      el("div", {}, el("dt", {}, "幂等键"), el("dd", {class: "mono"}, data.request_key.slice(0, 16))),
    ),
    table(["项目", "确认后归属卷宗", "电梯或槽位", "桶线索", "结论"],
      data.rows.map(r => [r.项目, r.归属卷宗, r.电梯或槽位, r.桶线索, r.结论]),
      "调拨后检证（按新归属）"),
  );
}

function orderDetail(orderId, reviewDate) {
  const mount = el("div", {class: "verification"});
  const issue = errorText();
  const dateInput = el("input", {name: "review_date", type: "date", required: true, value: reviewDate});
  const form = el("form", {}, field("巡检日", dateInput),
    el("button", {type: "submit"}, "查询调拨结果"), issue);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    issue.textContent = "";
    try {
      await orderVerification(orderId, dateInput.value, mount);
    } catch (error) {
      issue.textContent = error.message;
    }
  });
  // 首次进入即按今天出结论，体现“确认后按新归属”。
  orderVerification(orderId, reviewDate, mount).catch(error => {
    issue.textContent = error.message;
  });
  return el("div", {}, form, mount);
}

export async function transfersPage(config, user, route) {
  const [dossiers, orders] = await Promise.all([api.dossiers(), api.transferOrders()]);
  const isOperator = user.role === "operator";

  const dossierPanels = dossiers.map(d => section(`${d.code} · ${d.title}`, certTable(d)));
  const selectedOrder = route.id ? await api.transferOrder(route.id).catch(() => null) : null;

  return el("div", {class: "page transfers"},
    pageHeading(config.modules[2], "两份卷宗成批等量互换证书；整单要么全部生效，要么全部不生效。"),
    el("div", {class: "split"}, ...dossierPanels),
    selectedOrder
      ? section(`调拨单 #${selectedOrder.id} · 检证结果`, orderDetail(selectedOrder.id, today()))
      : section("发起调拨", isOperator ? swapForm(config, dossiers) :
        empty("只读账号可查看卷宗与调拨单，调拨由资料操作员发起。")),
    section("调拨点查（确认前旧归属 / 确认后新归属）", pointCheckPanel()),
    section("调拨单记录", orders.length ? ordersTable(orders) : empty("暂无调拨单。")));
}
