// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

using Microsoft.VisualStudio.Services.Agent.Util;
using Microsoft.Win32;
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.Versioning;

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
        private const string ServiceNamePrefix = "azpagent.autoreconnect.";
        private const string ServiceDisplayNamePrefix = "Azure Pipelines Agent Auto Reconnect ";

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

            string serviceName = GetAgentSpecificName(ServiceNamePrefix);
            string expectedImagePath = BuildServiceCommandLine(listenerExecutable);
            string actualImagePath = GetServiceImagePath(serviceName);

            if (_windowsServiceHelper.IsServiceExists(serviceName))
            {
                if (CommandLinesMatch(actualImagePath, expectedImagePath))
                {
                    Trace.Info($"Windows auto reconnect already registered as service '{serviceName}'.");
                    return;
                }

                Trace.Warning($"Windows auto reconnect service '{serviceName}' exists with a different image path. Reinstalling it.");
                _windowsServiceHelper.UninstallService(serviceName);
            }

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

        private string GetAgentSpecificName(string prefix)
        {
            return prefix + GetAgentPathHash();
        }

        private string GetAgentPathHash()
        {
            return IOUtil.GetPathHash(HostContext.GetDirectory(WellKnownDirectory.Root)).Substring(0, 10);
        }

        private static bool CommandLinesMatch(string actual, string expected)
        {
            return string.Equals(NormalizeCommandLine(actual), NormalizeCommandLine(expected), StringComparison.OrdinalIgnoreCase);
        }

        private static string NormalizeCommandLine(string value)
        {
            return (value ?? string.Empty).Trim().Trim('"');
        }

        private static string BuildServiceCommandLine(string listenerExecutable)
        {
            return "\"" + listenerExecutable + "\" run --startuptype service";
        }
    }
}
