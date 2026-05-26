import * as vscode from "vscode";

function quote(value: string): string {
  return value.includes(" ") ? `"${value}"` : value;
}

export function runCliInTerminal(cliPath: string, args: string[], cwd?: string) {
  const terminal = vscode.window.createTerminal({
    name: "Migration Agent",
    cwd
  });
  terminal.show();
  terminal.sendText([quote(cliPath), ...args.map(quote)].join(" "));
}

