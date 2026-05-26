import * as vscode from "vscode";
import { runCliInTerminal } from "./cliRunner";

function workspaceFolder(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

function cliPath(): string {
  return vscode.workspace.getConfiguration("migrationAgent").get<string>("cliPath", "migration-agent");
}

function targetFramework(): string {
  return vscode.workspace.getConfiguration("migrationAgent").get<string>("targetFramework", "net8.0");
}

export function activate(context: vscode.ExtensionContext) {
  context.subscriptions.push(
    vscode.commands.registerCommand("migrationAgent.listAgents", () => {
      runCliInTerminal(cliPath(), ["list", "agents"], workspaceFolder());
    }),
    vscode.commands.registerCommand("migrationAgent.runRepositoryAnalysis", async () => {
      const folder = workspaceFolder();
      if (!folder) {
        vscode.window.showErrorMessage("Open a workspace folder before running Migration Agent.");
        return;
      }
      const input = JSON.stringify({
        sourcePath: folder,
        targetFramework: targetFramework(),
        outputDir: "artifacts"
      }).replace(/"/g, '\\"');
      runCliInTerminal(cliPath(), ["run", "agent", "repository-analysis", "--input-json", input], folder);
    }),
    vscode.commands.registerCommand("migrationAgent.runWorkflow", () => {
      runCliInTerminal(cliPath(), ["run", "workflow", "--input", "samples/input/workflow-input.json"], workspaceFolder());
    }),
    vscode.commands.registerCommand("migrationAgent.openArtifacts", async () => {
      const folder = workspaceFolder();
      if (!folder) {
        return;
      }
      await vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(`${folder}/artifacts`), true);
    })
  );
}

export function deactivate() {}

