import type { UpdateState } from "./channels";

const OFFICIAL_RELEASE_URL = "https://github.com/Zleap-AI/SAG/releases/latest";

export interface UpdaterErrorPresentation {
  kind: "signature-mismatch" | "generic";
  title: string;
  message: string;
  detail: string;
  actionLabel?: string;
  actionUrl?: string;
}

export function shouldPresentUpdaterError(previousState: UpdateState): boolean {
  return previousState.status === "downloaded";
}

export function describeUpdaterError(error: unknown): UpdaterErrorPresentation {
  const errorMessage = error instanceof Error ? error.message : String(error);
  const signatureMismatch = /code signature.*did not pass validation/i.test(errorMessage);

  if (signatureMismatch) {
    return {
      kind: "signature-mismatch",
      title: "无法自动更新",
      message: "当前安装的 SAG 无法验证正式更新包的签名。",
      detail:
        "这通常发生在从本地测试版升级到正式签名版时。"
        + "请从官方发布页下载最新版并覆盖安装一次；现有数据不会被删除，"
        + "之后即可继续使用自动更新。",
      actionLabel: "打开官方下载页",
      actionUrl: OFFICIAL_RELEASE_URL,
    };
  }

  return {
    kind: "generic",
    title: "更新失败",
    message: "SAG 未能完成自动更新。",
    detail: errorMessage || "未知错误，请导出诊断日志后重试。",
  };
}
