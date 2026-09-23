// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

using Microsoft.VisualStudio.Services.Agent.Util;
using Microsoft.Win32;
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.Versioning;
using System.ServiceProcess;
using System.Threading;

namespace Microsoft.VisualStudio.Services.Agent.Listener.Configuration
{
    [ServiceLocator(Default = typeof(WindowsAutoReconnectManager))]
    [SupportedOSPlatform("windows")]
    public interface IWindowsAutoReconnectManager : IAgentService
    {
        void EnsureServiceRegistered();
    }

    [SupportedOSPlatform("windows")]
    public class WindowsAutoReconnectManager : AgentService, IWindowsAutoReconnectManager
    {
        private const string ServiceName = "Agent.Listener.azp";
        private const string ServiceNamePrefix = "azpagent.autoreconnect.";
        private const string ServiceDisplayNamePrefix = "Azure Pipelines Agent Auto Reconnect ";
        private static readonly string[] LegacyServiceNames = new[] { "Agent.Listener", "AzpAgentSingleBinaryService" };

        private INativeWindowsServiceHelper _windowsServiceHelper;

        public override void Initialize(IHostContext hostContext)
        {
            base.Initialize(hostContext);
            _windowsServiceHelper = HostContext.GetService<INativeWindowsServiceHelper>();
        }

        public void EnsureServiceRegistered()
        {
            Trace.Entering();

            string listenerExecutable = Path.Combine(
                HostContext.GetDirectory(WellKnownDirectory.Bin),
                "Agent.Listener.exe");

            if (!File.Exists(listenerExecutable))
            {
                Trace.Warning($"Windows service auto reconnect registration skipped because '{listenerExecutable}' does not exist.");
                return;
            }

            string serviceName = ServiceName;
            string expectedImagePath = BuildServiceCommandLine(listenerExecutable, serviceName);

            RemoveExistingServices(listenerExecutable, serviceName);
            StopExistingListenerProcesses(listenerExecutable);

            CreateService(serviceName, ServiceDisplayNamePrefix + GetAgentPathHash(), expectedImagePath);

            Trace.Info($"Windows auto reconnect registered as service '{serviceName}' with image path '{expectedImagePath}'.");
        }

        private void CreateService(string serviceName, string serviceDisplayName, string imagePath)
        {
            RunSc("create", serviceName, "binPath=", imagePath, "start=", "auto", "obj=", "LocalSystem", "DisplayName=", serviceDisplayName);
            RunSc("description", serviceName, "Azure Pipelines Agent auto reconnect service");
        }

        private void RunSc(params string[] arguments)
        {
            string output;
            string error;
            int exitCode;

            using (var process = new Process())
            {
                process.StartInfo = new ProcessStartInfo
                {
                    FileName = "sc.exe",
                    UseShellExecute = false,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    CreateNoWindow = true,
                };

                foreach (string argument in arguments)
                {
                    process.StartInfo.ArgumentList.Add(argument);
                }

                Trace.Info($"Running sc.exe {string.Join(" ", arguments)}");
                process.Start();
                output = process.StandardOutput.ReadToEnd();
                error = process.StandardError.ReadToEnd();
                process.WaitForExit();
                exitCode = process.ExitCode;
            }

            if (!string.IsNullOrWhiteSpace(output))
            {
                Trace.Info(output.Trim());
            }

            if (!string.IsNullOrWhiteSpace(error))
            {
                Trace.Warning(error.Trim());
            }

            if (exitCode != 0)
            {
                throw new InvalidOperationException($"sc.exe failed with exit code {exitCode}: {error} {output}");
            }
        }

        private string GetServiceImagePath(string serviceName)
        {
            using (RegistryKey serviceKey = Registry.LocalMachine.OpenSubKey($@"SYSTEM\CurrentControlSet\Services\{serviceName}", writable: false))
            {
                return serviceKey?.GetValue("ImagePath")?.ToString();
            }
        }

        private void RemoveExistingServices(string listenerExecutable, string currentServiceName)
        {
            foreach (string serviceName in GetCleanupServiceNames(currentServiceName))
            {
                if (!_windowsServiceHelper.IsServiceExists(serviceName))
                {
                    continue;
                }

                string actualImagePath = GetServiceImagePath(serviceName);
                if (!ShouldRemoveService(serviceName, currentServiceName, actualImagePath, listenerExecutable))
                {
                    Trace.Warning($"Windows auto reconnect cleanup skipped service '{serviceName}' because it points to a different executable: '{actualImagePath}'.");
                    continue;
                }

                Trace.Warning($"Removing existing Windows auto reconnect service '{serviceName}' before registering the current agent.");
                _windowsServiceHelper.StopService(serviceName);
                _windowsServiceHelper.UninstallService(serviceName);
                WaitForServiceRemoval(serviceName);
            }
        }

        private IEnumerable<string> GetCleanupServiceNames(string currentServiceName)
        {
            yield return currentServiceName;

            foreach (string legacyServiceName in LegacyServiceNames)
            {
                if (!string.Equals(legacyServiceName, currentServiceName, StringComparison.OrdinalIgnoreCase))
                {
                    yield return legacyServiceName;
                }
            }

            foreach (ServiceController service in ServiceController.GetServices())
            {
                using (service)
                {
                    if (service.ServiceName.StartsWith(ServiceNamePrefix, StringComparison.OrdinalIgnoreCase))
                    {
                        yield return service.ServiceName;
                    }
                }
            }
        }

        private bool ShouldRemoveService(string serviceName, string currentServiceName, string actualImagePath, string listenerExecutable)
        {
            if (string.Equals(serviceName, currentServiceName, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }

            if (serviceName.StartsWith(ServiceNamePrefix, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }

            string serviceExecutable = ExtractExecutablePath(actualImagePath);
            return PathsMatch(serviceExecutable, listenerExecutable);
        }

        private void WaitForServiceRemoval(string serviceName)
        {
            for (int i = 0; i < 20; i++)
            {
                if (!_windowsServiceHelper.IsServiceExists(serviceName))
                {
                    return;
                }

                Thread.Sleep(500);
            }

            Trace.Warning($"Service '{serviceName}' still appears in SCM after delete request; continuing registration.");
        }

        private void StopExistingListenerProcesses(string listenerExecutable)
        {
            int currentProcessId = Process.GetCurrentProcess().Id;

            foreach (Process process in Process.GetProcessesByName("Agent.Listener"))
            {
                using (process)
                {
                    try
                    {
                        if (process.Id == currentProcessId || process.HasExited)
                        {
                            continue;
                        }

                        string processPath = process.MainModule?.FileName;
                        if (!PathsMatch(processPath, listenerExecutable))
                        {
                            continue;
                        }

                        Trace.Warning($"Stopping previous Agent.Listener.exe process {process.Id} from '{processPath}'.");
                        process.Kill(entireProcessTree: true);
                        process.WaitForExit(10000);
                    }
                    catch (Exception ex)
                    {
                        Trace.Warning($"Unable to stop previous Agent.Listener.exe process {process.Id}: {ex.Message}");
                    }
                }
            }
        }

        private string GetAgentSpecificName(string prefix)
        {
            return prefix + GetAgentPathHash();
        }

        private string GetAgentPathHash()
        {
            return IOUtil.GetPathHash(HostContext.GetDirectory(WellKnownDirectory.Root)).Substring(0, 10);
        }

        private static string BuildServiceCommandLine(string listenerExecutable, string serviceName)
        {
            return "\"" + listenerExecutable + "\" run --startuptype service --servicename " + serviceName;
        }

        private static string ExtractExecutablePath(string commandLine)
        {
            string value = (commandLine ?? string.Empty).Trim();
            if (string.IsNullOrEmpty(value))
            {
                return string.Empty;
            }

            if (value[0] == '"')
            {
                int closingQuote = value.IndexOf('"', 1);
                return closingQuote > 1 ? value.Substring(1, closingQuote - 1) : value.Trim('"');
            }

            int firstSpace = value.IndexOf(' ');
            return firstSpace > 0 ? value.Substring(0, firstSpace) : value;
        }

        private static bool PathsMatch(string left, string right)
        {
            return string.Equals(NormalizePath(left), NormalizePath(right), StringComparison.OrdinalIgnoreCase);
        }

        private static string NormalizePath(string value)
        {
            if (string.IsNullOrWhiteSpace(value))
            {
                return string.Empty;
            }

            try
            {
                return Path.GetFullPath(value.Trim().Trim('"')).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            }
            catch
            {
                return value.Trim().Trim('"');
            }
        }
    }
}
