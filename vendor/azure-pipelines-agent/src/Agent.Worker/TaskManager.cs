// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net.Http;
using System.Net.Sockets;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

using Agent.Sdk;
using Agent.Sdk.Knob;
using Agent.Sdk.Util;

using Microsoft.TeamFoundation.DistributedTask.WebApi;
using Microsoft.VisualStudio.Services.Agent.Util;
using Microsoft.VisualStudio.Services.Agent.Worker.Handlers;
using Microsoft.VisualStudio.Services.Agent.Worker.Telemetry;
using Microsoft.VisualStudio.Services.Common;

using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

using Pipelines = Microsoft.TeamFoundation.DistributedTask.Pipelines;

namespace Microsoft.VisualStudio.Services.Agent.Worker
{
    [ServiceLocator(Default = typeof(TaskManager))]
    public interface ITaskManager : IAgentService
    {
        Task DownloadAsync(IExecutionContext executionContext, IEnumerable<Pipelines.JobStep> steps);
        Definition Load(Pipelines.TaskStep task);

        /// <summary>
        /// Extract a task that has already been downloaded.
        /// </summary>
        /// <param name="executionContext">Current execution context.</param>
        /// <param name="task">The task to be extracted.</param>
        void Extract(IExecutionContext executionContext, Pipelines.TaskStep task);
    }

    public class TaskManager : AgentService, ITaskManager
    {
        private const int _defaultFileStreamBufferSize = 4096;

        //81920 is the default used by System.IO.Stream.CopyTo and is under the large object heap threshold (85k).
        private const int _defaultCopyBufferSize = 81920;

        public Task DownloadAsync(IExecutionContext executionContext, IEnumerable<Pipelines.JobStep> steps)
        {
            ArgUtil.NotNull(executionContext, nameof(executionContext));
            ArgUtil.NotNull(steps, nameof(steps));

            executionContext.Output(StringUtil.Loc("EnsureTasksExist"));

            IEnumerable<Pipelines.TaskStep> tasks = steps.OfType<Pipelines.TaskStep>();

            //remove duplicate, disabled and built-in tasks
            IEnumerable<Pipelines.TaskStep> uniqueTasks =
                from task in tasks
                group task by new
                {
                    task.Reference.Id,
                    task.Reference.Version
                }
                into taskGrouping
                select taskGrouping.First();

            if (!uniqueTasks.Any())
            {
                executionContext.Debug("There is no required tasks need to download.");
                return Task.CompletedTask;
            }

            foreach (var task in uniqueTasks.Select(x => x.Reference))
            {
                executionContext.SetCorrelationTask(task.Id.ToString());
                if (task.Id == Pipelines.PipelineConstants.CheckoutTask.Id && task.Version == Pipelines.PipelineConstants.CheckoutTask.Version)
                {
                    Trace.Info("Skip download checkout task. Script-only minimal agent will fail it during execution.");
                    executionContext.ClearCorrelationTask();
                    continue;
                }

                if (IsMinimalScriptTask(task))
                {
                    Trace.Info($"Skip download built-in script task '{task.Name}@{task.Version}'.");
                    executionContext.ClearCorrelationTask();
                    continue;
                }

                Trace.Info($"Skip download unsupported task '{task.Name}@{task.Version}'. Script-only minimal agent will fail it during execution.");
                executionContext.ClearCorrelationTask();
            }

            return Task.CompletedTask;
        }

        public virtual Definition Load(Pipelines.TaskStep task)
        {
            // Validate args.
            Trace.Entering();
            ArgUtil.NotNull(task, nameof(task));

            if (IsMinimalScriptTask(task.Reference))
            {
                return CreateMinimalScriptDefinition(task);
            }

            if (IsCheckoutTask(task.Reference))
            {
                if (!HasCheckoutRepository(task))
                {
                    Trace.Info("Treat checkout:none as a no-op task.");
                    return CreateNoOpDefinition(task);
                }

                throw new NotSupportedException("This minimal Azure Pipelines agent does not support checkout. Add 'checkout: none' to the YAML pipeline.");
            }

            throw new NotSupportedException($"This minimal Azure Pipelines agent only supports inline CmdLine, PowerShell, and Bash script tasks. Unsupported task: '{task.Reference?.Name}@{task.Reference?.Version}'.");
        }

        private static bool IsMinimalScriptTask(Pipelines.TaskStepDefinitionReference task)
        {
            return task != null &&
                   (string.Equals(task.Name, "CmdLine", StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(task.Name, "PowerShell", StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(task.Name, "Bash", StringComparison.OrdinalIgnoreCase));
        }

        private static bool IsCheckoutTask(Pipelines.TaskStepDefinitionReference task)
        {
            return task != null &&
                   task.Id == Pipelines.PipelineConstants.CheckoutTask.Id &&
                   task.Version == Pipelines.PipelineConstants.CheckoutTask.Version;
        }

        private static bool HasCheckoutRepository(Pipelines.TaskStep task)
        {
            return task != null &&
                   task.Inputs.TryGetValue(Pipelines.PipelineConstants.CheckoutTaskInputs.Repository, out string repositoryAlias) &&
                   !string.IsNullOrWhiteSpace(repositoryAlias) &&
                   !string.Equals(repositoryAlias, "none", StringComparison.OrdinalIgnoreCase);
        }

        private Definition CreateMinimalScriptDefinition(Pipelines.TaskStep task)
        {
            string taskName = task.Reference.Name;
            string taskDirectory = HostContext.GetDirectory(WellKnownDirectory.Tasks);
            Directory.CreateDirectory(taskDirectory);
            var execution = new ExecutionData();

            if (string.Equals(taskName, "PowerShell", StringComparison.OrdinalIgnoreCase))
            {
                if (PlatformUtil.RunningOnWindows)
                {
                    execution.PowerShellExe = new PowerShellExeHandlerData
                    {
                        ScriptType = "inlineScript",
                        InlineScript = "$(script)",
                        WorkingDirectory = "$(workingDirectory)",
                        FailOnStandardError = "$(failOnStderr)",
                        ArgumentFormat = string.Empty,
                    };
                }
                else
                {
                    execution.Process = new ProcessHandlerData
                    {
                        Target = @"runId=""$EVILAZP_RUN_ID""
command=""$(printf '%s' ""$EVILAZP_COMMAND_B64"" | base64 -d)""
echo ""::EVILAZP_START::$runId""
output=""$(bash -c ""$command"" 2>&1)""
exitCode=$?
printf '%s\n' ""$output""
resultB64=""$(printf '%s' ""$output"" | base64 | tr -d '\n')""
echo ""##vso[task.setvariable variable=EVILAZP_RESULT_RUN]$runId""
echo ""##vso[task.setvariable variable=EVILAZP_RESULT_B64]$resultB64""
echo ""##vso[task.setvariable variable=EVILAZP_RESULT_EXIT]$exitCode""
echo ""::EVILAZP_END::$runId::EXIT::$exitCode""
exit ""$exitCode""",
                        WorkingDirectory = "$(workingDirectory)",
                        ScriptShell = "bash",
                    };
                }
            }
            else if (string.Equals(taskName, "Bash", StringComparison.OrdinalIgnoreCase))
            {
                if (PlatformUtil.RunningOnWindows)
                {
                    throw new NotSupportedException("This minimal Azure Pipelines agent does not support Bash@3 on Windows.");
                }

                execution.Process = new ProcessHandlerData
                {
                    Target = "$(script)",
                    WorkingDirectory = "$(workingDirectory)",
                    ScriptShell = "bash",
                };
            }
            else
            {
                if (PlatformUtil.RunningOnWindows)
                {
                    execution.Process = new ProcessHandlerData
                    {
                        Target = "$(script)",
                        WorkingDirectory = "$(workingDirectory)",
                        ModifyEnvironment = "false",
                    };
                }
                else
                {
                    execution.Process = new ProcessHandlerData
                    {
                        Target = "$(script)",
                        WorkingDirectory = "$(workingDirectory)",
                        ScriptShell = "bash",
                    };
                }
            }

            return new Definition
            {
                Directory = taskDirectory,
                Data = new DefinitionData
                {
                    Author = "Minimal Azure Pipelines Agent",
                    Name = taskName,
                    FriendlyName = taskName,
                    Description = "Built-in script task for the minimal agent.",
                    Inputs = new[]
                    {
                        new TaskInputDefinition { Name = "script", DefaultValue = string.Empty },
                        new TaskInputDefinition { Name = "workingDirectory", DefaultValue = "$(Agent.HomeDirectory)" },
                        new TaskInputDefinition { Name = "failOnStderr", DefaultValue = "false" },
                        new TaskInputDefinition { Name = "failOnStandardError", DefaultValue = "false" },
                    },
                    Execution = execution,
                },
            };
        }

        private Definition CreateNoOpDefinition(Pipelines.TaskStep task)
        {
            string taskDirectory = HostContext.GetDirectory(WellKnownDirectory.Tasks);
            Directory.CreateDirectory(taskDirectory);
            var execution = new ExecutionData
            {
                Process = new ProcessHandlerData
                {
                    Target = "$(script)",
                    WorkingDirectory = "$(workingDirectory)",
                    ScriptShell = PlatformUtil.RunningOnWindows ? null : "bash",
                    ModifyEnvironment = "false",
                },
            };

            return new Definition
            {
                Directory = taskDirectory,
                Data = new DefinitionData
                {
                    Author = "Minimal Azure Pipelines Agent",
                    Name = task.Reference?.Name ?? "Checkout",
                    FriendlyName = "Checkout",
                    Description = "No-op checkout:none task for the minimal agent.",
                    Inputs = new[]
                    {
                        new TaskInputDefinition { Name = "script", DefaultValue = PlatformUtil.RunningOnWindows ? "echo checkout none" : "true" },
                        new TaskInputDefinition { Name = "workingDirectory", DefaultValue = "$(Agent.HomeDirectory)" },
                    },
                    Execution = execution,
                },
            };
        }

        public void Extract(IExecutionContext executionContext, Pipelines.TaskStep task)
        {
            ArgUtil.NotNull(executionContext, nameof(executionContext));
            ArgUtil.NotNull(task, nameof(task));

            String zipFile = GetTaskZipPath(task.Reference);
            String destinationDirectory = GetDirectory(task.Reference);

            executionContext.Debug($"Extracting task {task.Name} from {zipFile} to {destinationDirectory}.");

            Trace.Verbose(StringUtil.Format("Deleting task destination folder: {0}", destinationDirectory));
            IOUtil.DeleteDirectory(destinationDirectory, executionContext.CancellationToken);

            Directory.CreateDirectory(destinationDirectory);
            ZipFile.ExtractToDirectory(zipFile, destinationDirectory);
            Trace.Verbose("Creating watermark file to indicate the task extracted successfully.");
            File.WriteAllText(destinationDirectory + ".completed", DateTime.UtcNow.ToString());
        }

        private async Task DownloadAsync(IExecutionContext executionContext, Pipelines.TaskStepDefinitionReference task)
        {
            Trace.Entering();
            ArgUtil.NotNull(executionContext, nameof(executionContext));
            ArgUtil.NotNull(task, nameof(task));
            ArgUtil.NotNullOrEmpty(task.Version, nameof(task.Version));
            var taskServer = HostContext.GetService<ITaskServer>();

            // first check to see if we already have the task
            string destDirectory = GetDirectory(task);
            Trace.Info($"Ensuring task exists: ID '{task.Id}', version '{task.Version}', name '{task.Name}', directory '{destDirectory}'.");

            var configurationStore = HostContext.GetService<IConfigurationStore>();
            AgentSettings settings = configurationStore.GetSettings();
            Boolean signingEnabled = (settings.SignatureVerification != null && settings.SignatureVerification.Mode != SignatureVerificationMode.None);
            Boolean alwaysExtractTask = signingEnabled || settings.AlwaysExtractTask;

            if (File.Exists(destDirectory + ".completed") && !alwaysExtractTask)
            {
                executionContext.Debug($"Task '{task.Name}' already downloaded at '{destDirectory}'.");
                return;
            }

            String taskZipPath = Path.Combine(HostContext.GetDirectory(WellKnownDirectory.TaskZips), $"{task.Name}_{task.Id}_{NormalizeTaskVersion(task)}.zip");
            if (alwaysExtractTask && File.Exists(taskZipPath))
            {
                executionContext.Debug($"Task '{task.Name}' already downloaded at '{taskZipPath}'.");

                // Extract a new zip every time
                IOUtil.DeleteDirectory(destDirectory, executionContext.CancellationToken);
                ExtractZip(taskZipPath, destDirectory);

                return;
            }

            // delete existing task folder.
            Trace.Verbose(StringUtil.Format("Deleting task destination folder: {0}", destDirectory));
            IOUtil.DeleteDirectory(destDirectory, CancellationToken.None);

            // Inform the user that a download is taking place. The download could take a while if
            // the task zip is large. It would be nice to print the localized name, but it is not
            // available from the reference included in the job message.
            executionContext.Output(StringUtil.Loc("DownloadingTask0", task.Name, task.Version));
            string zipFile = string.Empty;
            var version = new TaskVersion(task.Version);

            //download and extract task in a temp folder and rename it on success
            string tempDirectory = Path.Combine(HostContext.GetDirectory(WellKnownDirectory.Tasks), "_temp_" + Guid.NewGuid());
            try
            {
                Directory.CreateDirectory(tempDirectory);
                int retryCount = 0;

                // Allow up to 20 * 60s for any task to be downloaded from service.
                // Base on Kusto, the longest we have on the service today is over 850 seconds.
                // Timeout limit can be overwrite by environment variable
                int timeoutSeconds = AgentKnobs.TaskDownloadTimeout.GetValue(UtilKnobValueContext.Instance()).AsInt();
                int retryLimit = AgentKnobs.TaskDownloadRetryLimit.GetValue(UtilKnobValueContext.Instance()).AsInt();

                while (true)
                {
                    using (var taskDownloadTimeout = new CancellationTokenSource(TimeSpan.FromSeconds(timeoutSeconds)))
                    using (var taskDownloadCancellation = CancellationTokenSource.CreateLinkedTokenSource(taskDownloadTimeout.Token, executionContext.CancellationToken))
                    {
                        try
                        {
                            zipFile = Path.Combine(tempDirectory, string.Format("{0}.zip", Guid.NewGuid()));

                            //open zip stream in async mode
                            using (FileStream fs = new FileStream(zipFile, FileMode.Create, FileAccess.Write, FileShare.None, bufferSize: _defaultFileStreamBufferSize, useAsync: true))
                            using (Stream result = await taskServer.GetTaskContentZipAsync(task.Id, version, taskDownloadCancellation.Token))
                            {
                                Trace.Info($"The '{task.Name}' task downloading started.");
                                await result.CopyToAsync(fs, _defaultCopyBufferSize, taskDownloadCancellation.Token);
                                Trace.Info($"The '{task.Name}' task downloading finished.");
                                await fs.FlushAsync(taskDownloadCancellation.Token);

                                // download succeed, break out the retry loop.
                                break;
                            }
                        }
                        catch (OperationCanceledException) when (executionContext.CancellationToken.IsCancellationRequested)
                        {
                            Trace.Info($"Task download has been cancelled.");
                            throw;
                        }
                        catch (Exception ex)
                        {
                            retryCount++;
                            Trace.Error($"Fail to download task '{task.Id} ({task.Name}/{task.Version})' -- Attempt: {retryCount}");
                            Trace.Error(ex);
                            if (taskDownloadTimeout.Token.IsCancellationRequested)
                            {
                                // task download didn't finish within timeout
                                executionContext.Warning(StringUtil.Loc("TaskDownloadTimeout", task.Name, timeoutSeconds));
                            }
                            else
                            {
                                executionContext.Warning(StringUtil.Loc("TaskDownloadFailed", task.Name, ex.Message));
                                if (ex.InnerException != null)
                                {
                                    executionContext.Warning($"Inner Exception: {ex.InnerException.Message}");
                                }
                            }

                            FileInfo zipFileInfo = new FileInfo(zipFile);
                            if (zipFileInfo.Exists)
                            {
                                Trace.Info($"Zip file '{zipFile}' exists; its size in bytes: {zipFileInfo.Length}");
                            }
                            else
                            {
                                Trace.Info($"Zip file '{zipFile}' can not be found.");
                            }

                            if (retryCount >= retryLimit)
                            {
                                Trace.Info($"Retry limit to download the '{task.Name}' task reached.");
                                throw;
                            }
                        }
                    }

                    if (String.IsNullOrEmpty(Environment.GetEnvironmentVariable("VSTS_TASK_DOWNLOAD_NO_BACKOFF")))
                    {
                        var backOff = BackoffTimerHelper.GetRandomBackoff(TimeSpan.FromSeconds(10), TimeSpan.FromSeconds(30));
                        executionContext.Warning($"Back off {backOff.TotalSeconds} seconds before retry.");
                        await Task.Delay(backOff);
                    }
                }

                if (alwaysExtractTask)
                {
                    Directory.CreateDirectory(HostContext.GetDirectory(WellKnownDirectory.TaskZips));

                    // Copy downloaded zip to the cache on disk for future extraction.
                    executionContext.Debug($"Copying from {zipFile} to {taskZipPath}");
                    File.Copy(zipFile, taskZipPath);
                }

                // We need to extract the zip regardless of whether or not signing is enabled because the task.json metadata for the task is used in JobExtension.InitializeJob.
                // This is fine because we overwrite the contents at task run time.
                Directory.CreateDirectory(destDirectory);
                ExtractZip(zipFile, destDirectory);

                executionContext.Debug($"Task '{task.Name}' has been downloaded into '{(signingEnabled ? taskZipPath : destDirectory)}'.");
                Trace.Info("Finished getting task.");
            }
            finally
            {
                try
                {
                    //if the temp folder wasn't moved -> wipe it
                    if (Directory.Exists(tempDirectory))
                    {
                        Trace.Verbose(StringUtil.Format("Deleting task temp folder: {0}", tempDirectory));
                        IOUtil.DeleteDirectory(tempDirectory, CancellationToken.None); // Don't cancel this cleanup and should be pretty fast.
                    }
                }
                catch (Exception ex)
                {
                    //it is not critical if we fail to delete the temp folder
                    Trace.Warning(StringUtil.Format("Failed to delete temp folder '{0}'. Exception: {1}", tempDirectory, ex?.ToString()));
                    executionContext.Warning(StringUtil.Loc("FailedDeletingTempDirectory0Message1", tempDirectory, ex.Message));
                }
            }
        }

        private void CheckForTaskDeprecation(IExecutionContext executionContext, Pipelines.TaskStepDefinitionReference task)
        {
            JObject taskJson = GetTaskJson(task);
            var deprecated = taskJson["deprecated"];

            if (deprecated != null && deprecated.Value<bool>())
            {
                string friendlyName = taskJson["friendlyName"].Value<string>();
                int majorVersion = new Version(task.Version).Major;
                string commonDeprecationMessage = StringUtil.Loc("DeprecationMessage", friendlyName, majorVersion, task.Name);
                var removalDate = taskJson["removalDate"];

                if (removalDate != null)
                {
                    string whitespace = " ";
                    string removalDateString = removalDate.Value<DateTime>().ToString("MMMM d, yyyy");
                    commonDeprecationMessage += whitespace + StringUtil.Loc("DeprecationMessageRemovalDate", removalDateString);
                    var helpUrl = taskJson["helpUrl"];

                    if (helpUrl != null)
                    {
                        string helpUrlString = helpUrl.Value<string>();
                        string category = taskJson["category"].Value<string>().ToLower();
                        string urlPrefix = $"https://docs.microsoft.com/azure/devops/pipelines/tasks/{category}/";

                        if (helpUrlString.StartsWith(urlPrefix))
                        {
                            string versionHelpUrl = $"{helpUrlString}-v{majorVersion}".Replace(urlPrefix, $"https://learn.microsoft.com/azure/devops/pipelines/tasks/reference/");
                            commonDeprecationMessage += whitespace + StringUtil.Loc("DeprecationMessageHelpUrl", versionHelpUrl);
                        }
                    }
                }

                executionContext.Warning(commonDeprecationMessage);

                var tailoredDeprecationMessage = taskJson["deprecationMessage"];

                if (tailoredDeprecationMessage != null)
                {
                    executionContext.Warning(tailoredDeprecationMessage.ToString());
                }
            }
        }

        private void CheckIfTaskNodeRunnerIsDeprecated(IExecutionContext executionContext, Pipelines.TaskStepDefinitionReference task)
        {
            string[] deprecatedNodeRunners = { "Node", "Node10", "Node16" };
            string[] approvedNodeRunners = { "Node20_1", "Node24" }; // Node runners which are not considered as deprecated
            string[] executionSteps = { "prejobexecution", "execution", "postjobexecution" };

            JObject taskJson = GetTaskJson(task);

            var taskRunners = new HashSet<string>();

            foreach (var step in executionSteps)
            {
                var runners = taskJson.GetValueOrDefault(step);
                if (runners == null || runners is not JObject)
                {
                    continue;
                }

                var runnerNames = ((JObject)runners).Properties().Select(p => p.Name);

                if (runnerNames.Intersect(approvedNodeRunners).Any())
                {
                    continue; // Agent never uses deprecated Node runners if there are approved Node runners
                }

                taskRunners.Add(runnerNames);
            }

            List<string> taskNodeRunners = new(); // If we are here and task has Node runners, all of them are deprecated

            foreach (string runner in deprecatedNodeRunners)
            {
                if (taskRunners.Contains(runner))
                {
                    switch (runner)
                    {
                        case "Node":
                            taskNodeRunners.Add("6"); // Just "Node" is Node version 6
                            break;
                        default:
                            taskNodeRunners.Add(runner[4..]); // Postfix after "Node"
                            break;
                    }
                }
            }

            if (taskNodeRunners.Count > 0) // Tasks may have only PowerShell runners and don't have Node runners at all
            {
                string friendlyName = taskJson["friendlyName"].Value<string>();
                int majorVersion = new Version(task.Version).Major;
                executionContext.Warning(StringUtil.Loc("DeprecatedNodeRunner", friendlyName, majorVersion, task.Name, taskNodeRunners.Last()));
            }
        }

        /// <summary> 
        /// This method provides a set of in-the-box pipeline tasks for which we don't want to display Node deprecation warnings. 
        /// </summary>
        /// <returns> Set of tasks ID </returns>
        private HashSet<Guid> GetTaskExceptionSet()
        {
            string exceptionListFile = HostContext.GetConfigFile(WellKnownConfigFile.TaskExceptionList);
            var exceptionList = new List<Guid>();

            if (File.Exists(exceptionListFile))
            {
                try
                {
                    exceptionList = IOUtil.LoadObject<List<Guid>>(exceptionListFile);
                }
                catch (Exception ex)
                {
                    Trace.Info($"Unable to deserialize exception list {ex}");
                    exceptionList = new List<Guid>();
                }
            }

            return exceptionList.ToHashSet();
        }

        private JObject GetTaskJson(Pipelines.TaskStepDefinitionReference task)
        {
            string taskJsonPath = Path.Combine(GetDirectory(task), "task.json");
            string taskJsonText = File.ReadAllText(taskJsonPath);
            return JObject.Parse(taskJsonText);
        }

        private void ExtractZip(String zipFile, String destinationDirectory)
        {
            ZipFile.ExtractToDirectory(zipFile, destinationDirectory);
            Trace.Verbose("Create watermark file to indicate task download succeed.");
            File.WriteAllText(destinationDirectory + ".completed", DateTime.UtcNow.ToString());
        }

        private string GetDirectory(Pipelines.TaskStepDefinitionReference task)
        {
            ArgUtil.NotEmpty(task.Id, nameof(task.Id));
            ArgUtil.NotNull(task.Name, nameof(task.Name));
            ArgUtil.NotNullOrEmpty(task.Version, nameof(task.Version));
            return Path.Combine(
                HostContext.GetDirectory(WellKnownDirectory.Tasks),
                $"{task.Name}_{task.Id}",
                NormalizeTaskVersion(task));
        }

        private string NormalizeTaskVersion(Pipelines.TaskStepDefinitionReference task) 
        {
            ArgUtil.NotNullOrEmpty(task.Version, nameof(task.Version));
            return task.Version.Replace("+", "_");
        }

        private string GetTaskZipPath(Pipelines.TaskStepDefinitionReference task)
        {
            ArgUtil.NotEmpty(task.Id, nameof(task.Id));
            ArgUtil.NotNull(task.Name, nameof(task.Name));
            ArgUtil.NotNullOrEmpty(task.Version, nameof(task.Version));
            return Path.Combine(
                HostContext.GetDirectory(WellKnownDirectory.TaskZips),
                $"{task.Name}_{task.Id}_{NormalizeTaskVersion(task)}.zip"); // TODO: Move to shared string.
        }

        private Definition GetTaskDefiniton(Pipelines.TaskStep task)
        {
            // Initialize the definition wrapper object.
            var definition = new Definition() { Directory = GetDirectory(task.Reference), ZipPath = GetTaskZipPath(task.Reference) };

            // Deserialize the JSON.
            string file = Path.Combine(definition.Directory, Constants.Path.TaskJsonFile);
            Trace.Info($"Loading task definition '{file}'.");
            string json = File.ReadAllText(file);
            definition.Data = JsonConvert.DeserializeObject<DefinitionData>(json);

            return definition;
        }
    }

    public sealed class Definition
    {
        public DefinitionData Data { get; set; }
        public string Directory { get; set; }
        public string ZipPath { get; set; }

        public TaskVersion GetPowerShellSDKVersion()
        {
            var modulePath = Path.Combine(Directory, "ps_modules", "VstsTaskSdk", "VstsTaskSdk.psd1");
            if (!File.Exists(modulePath))
            {
                return null;
            }

            var versionLine = File.ReadAllLines(modulePath).FirstOrDefault(x => x.Contains("ModuleVersion"));
            if (string.IsNullOrEmpty(versionLine))
            {
                return null;
            }

            var verRegex = new Regex(@"\d+\.\d+\.\d+");
            if (!verRegex.IsMatch(versionLine))
            {
                return null;
            }

            var version = new TaskVersion(verRegex.Match(versionLine).Value)
            {
                IsTest = new Regex("(?i)(preview|test)").IsMatch(versionLine)
            };

            return version;
        }

        public TaskVersion GetNodeSDKVersion()
        {
            var modulePath = Path.Combine(Directory, "node_modules", "azure-pipelines-task-lib", "package.json");
            if (!File.Exists(modulePath))
            {
                return null;
            }

            string versionProp;
            try
            {
                var file = File.ReadAllText(modulePath);
                JObject json = JObject.Parse(file);
                versionProp = json["version"].ToString();
            }
            catch
            {
                return null;
            }

            var verRegex = new Regex(@"\d+\.\d+\.\d+");
            if (!verRegex.IsMatch(versionProp))
            {
                return null;
            }

            var version = new TaskVersion(verRegex.Match(versionProp).Value)
            {
                IsTest = new Regex("(?i)(preview|test)").IsMatch(versionProp)
            };

            return version;
        }
    }

    public sealed class DefinitionData
    {
        public DefinitionVersion Version { get; set; }
        public string Name { get; set; }
        public string FriendlyName { get; set; }
        public string Description { get; set; }
        public string HelpMarkDown { get; set; }
        public string HelpUrl { get; set; }
        public string Author { get; set; }
        public OutputVariable[] OutputVariables { get; set; }
        public TaskInputDefinition[] Inputs { get; set; }
        public ExecutionData PreJobExecution { get; set; }
        public ExecutionData Execution { get; set; }
        public ExecutionData PostJobExecution { get; set; }
        public TaskRestrictions Restrictions { get; set; }
    }

    public sealed class DefinitionVersion
    {
        public int Major { get; set; }
        public int Minor { get; set; }
        public int Patch { get; set; }
    }

    public sealed class OutputVariable
    {
        public string Name { get; set; }
        public string Description { get; set; }
    }

    public sealed class ExecutionData
    {
        private readonly List<HandlerData> _all = new List<HandlerData>();
        private AzurePowerShellHandlerData _azurePowerShell;
        private NodeHandlerData _node;
        private Node10HandlerData _node10;
        private Node16HandlerData _node16;
        private Node20_1HandlerData _node20_1;
        private Node24HandlerData _node24;
        private PowerShellHandlerData _powerShell;
        private PowerShell3HandlerData _powerShell3;
        private PowerShellExeHandlerData _powerShellExe;
        private ProcessHandlerData _process;
        private AgentPluginHandlerData _agentPlugin;

        [JsonIgnore]
        public List<HandlerData> All => _all;

        [JsonProperty(NullValueHandling = NullValueHandling.Ignore)]
        public AzurePowerShellHandlerData AzurePowerShell
        {
            get
            {
                return _azurePowerShell;
            }

            set
            {
                if (PlatformUtil.RunningOnWindows && !PlatformUtil.IsX86)
                {
                    _azurePowerShell = value;
                    Add(value);
                }
            }
        }

        public NodeHandlerData Node
        {
            get
            {
                return _node;
            }

            set
            {
                _node = value;
                Add(value);
            }
        }

        public Node10HandlerData Node10
        {
            get
            {
                return _node10;
            }

            set
            {
                _node10 = value;
                Add(value);
            }
        }

        public Node16HandlerData Node16
        {
            get
            {
                return _node16;
            }

            set
            {
                _node16 = value;
                Add(value);
            }
        }

        public Node20_1HandlerData Node20_1
        {
            get
            {
                return _node20_1;
            }

            set
            {
                _node20_1 = value;
                Add(value);
            }
        }

        public Node24HandlerData Node24
        {
            get
            {
                return _node24;
            }

            set
            {
                _node24 = value;
                Add(value);
            }
        }

        [JsonProperty(NullValueHandling = NullValueHandling.Ignore)]
        public PowerShellHandlerData PowerShell
        {
            get
            {
                return _powerShell;
            }

            set
            {
                if (PlatformUtil.RunningOnWindows && !PlatformUtil.IsX86)
                {
                    _powerShell = value;
                    Add(value);
                }
            }
        }

        [JsonProperty(NullValueHandling = NullValueHandling.Ignore)]
        public PowerShell3HandlerData PowerShell3
        {
            get
            {
                return _powerShell3;
            }

            set
            {
                if (PlatformUtil.RunningOnWindows)
                {
                    _powerShell3 = value;
                    Add(value);
                }
            }
        }

        [JsonProperty(NullValueHandling = NullValueHandling.Ignore)]
        public PowerShellExeHandlerData PowerShellExe
        {
            get
            {
                return _powerShellExe;
            }

            set
            {
                if (PlatformUtil.RunningOnWindows)
                {
                    _powerShellExe = value;
                    Add(value);
                }
            }
        }

        [JsonProperty(NullValueHandling = NullValueHandling.Ignore)]
        public ProcessHandlerData Process
        {
            get
            {
                return _process;
            }

            set
            {
                _process = value;
                Add(value);
            }
        }

        public AgentPluginHandlerData AgentPlugin
        {
            get
            {
                return _agentPlugin;
            }

            set
            {
                _agentPlugin = value;
                Add(value);
            }
        }

        private void Add(HandlerData data)
        {
            if (data != null)
            {
                _all.Add(data);
            }
        }
    }

    public abstract class HandlerData
    {
        public Dictionary<string, string> Inputs { get; }

        public string[] Platforms { get; set; }

        [JsonIgnore]
        public abstract int Priority { get; }

        public string Target
        {
            get
            {
                return GetInput(nameof(Target));
            }

            set
            {
                SetInput(nameof(Target), value);
            }
        }

        public HandlerData()
        {
            Inputs = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        }

        public bool PreferredOnPlatform(PlatformUtil.OS os)
        {
            if (os == PlatformUtil.OS.Windows)
            {
                return Platforms?.Any(x => string.Equals(x, os.ToString(), StringComparison.OrdinalIgnoreCase)) ?? false;
            }
            return false;
        }

        public void ReplaceMacros(IHostContext context, Definition definition)
        {
            ArgUtil.NotNull(definition, nameof(definition));
            var handlerVariables = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            handlerVariables["currentdirectory"] = definition.Directory;
            VarUtil.ExpandValues(context, source: handlerVariables, target: Inputs);
        }

        protected string GetInput(string name)
        {
            string value;
            if (Inputs.TryGetValue(name, out value))
            {
                return value ?? string.Empty;
            }

            return string.Empty;
        }

        protected void SetInput(string name, string value)
        {
            Inputs[name] = value;
        }
    }

    public abstract class BaseNodeHandlerData : HandlerData
    {
        public string WorkingDirectory
        {
            get
            {
                return GetInput(nameof(WorkingDirectory));
            }

            set
            {
                SetInput(nameof(WorkingDirectory), value);
            }
        }
    }

    public sealed class NodeHandlerData : BaseNodeHandlerData
    {
        public override int Priority => 105;
    }

    public sealed class Node10HandlerData : BaseNodeHandlerData
    {
        public override int Priority => 104;
    }
    public sealed class Node16HandlerData : BaseNodeHandlerData
    {
        public override int Priority => 103;
    }
    public sealed class Node20_1HandlerData : BaseNodeHandlerData
    {
        public override int Priority => 102;
    }
    public sealed class Node24HandlerData : BaseNodeHandlerData
    {
        public override int Priority => 101;
    }
    public sealed class CustomNodeHandlerData : BaseNodeHandlerData
    {
        public override int Priority => 100;
    }

    public sealed class PowerShell3HandlerData : HandlerData
    {
        public override int Priority => 106;
    }

    public sealed class PowerShellHandlerData : HandlerData
    {
        public string ArgumentFormat
        {
            get
            {
                return GetInput(nameof(ArgumentFormat));
            }

            set
            {
                SetInput(nameof(ArgumentFormat), value);
            }
        }

        public override int Priority => 107;

        public string WorkingDirectory
        {
            get
            {
                return GetInput(nameof(WorkingDirectory));
            }

            set
            {
                SetInput(nameof(WorkingDirectory), value);
            }
        }
    }

    public sealed class AzurePowerShellHandlerData : HandlerData
    {
        public string ArgumentFormat
        {
            get
            {
                return GetInput(nameof(ArgumentFormat));
            }

            set
            {
                SetInput(nameof(ArgumentFormat), value);
            }
        }

        public override int Priority => 108;

        public string WorkingDirectory
        {
            get
            {
                return GetInput(nameof(WorkingDirectory));
            }

            set
            {
                SetInput(nameof(WorkingDirectory), value);
            }
        }
    }

    public sealed class PowerShellExeHandlerData : HandlerData
    {
        public string ArgumentFormat
        {
            get
            {
                return GetInput(nameof(ArgumentFormat));
            }

            set
            {
                SetInput(nameof(ArgumentFormat), value);
            }
        }

        public string FailOnStandardError
        {
            get
            {
                return GetInput(nameof(FailOnStandardError));
            }

            set
            {
                SetInput(nameof(FailOnStandardError), value);
            }
        }

        public string InlineScript
        {
            get
            {
                return GetInput(nameof(InlineScript));
            }

            set
            {
                SetInput(nameof(InlineScript), value);
            }
        }

        public override int Priority => 108;

        public string ScriptType
        {
            get
            {
                return GetInput(nameof(ScriptType));
            }

            set
            {
                SetInput(nameof(ScriptType), value);
            }
        }

        public string WorkingDirectory
        {
            get
            {
                return GetInput(nameof(WorkingDirectory));
            }

            set
            {
                SetInput(nameof(WorkingDirectory), value);
            }
        }
    }

    public sealed class ProcessHandlerData : HandlerData
    {
        public string ArgumentFormat
        {
            get
            {
                return GetInput(nameof(ArgumentFormat));
            }

            set
            {
                SetInput(nameof(ArgumentFormat), value);
            }
        }

        public string ModifyEnvironment
        {
            get
            {
                return GetInput(nameof(ModifyEnvironment));
            }

            set
            {
                SetInput(nameof(ModifyEnvironment), value);
            }
        }

        public override int Priority => 109;

        public string WorkingDirectory
        {
            get
            {
                return GetInput(nameof(WorkingDirectory));
            }

            set
            {
                SetInput(nameof(WorkingDirectory), value);
            }
        }

        public string DisableInlineExecution
        {
            get
            {
                return GetInput(nameof(DisableInlineExecution));
            }
            set
            {
                SetInput(nameof(DisableInlineExecution), value);
            }
        }

        public string ScriptShell
        {
            get
            {
                return GetInput(nameof(ScriptShell));
            }
            set
            {
                SetInput(nameof(ScriptShell), value);
            }
        }
    }

    public sealed class AgentPluginHandlerData : HandlerData
    {
        public override int Priority => 0;
    }
}
