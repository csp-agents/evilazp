// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

using Microsoft.VisualStudio.Services.Agent;
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.Versioning;
using System.ServiceProcess;
using System.Threading;
using System.Threading.Tasks;

namespace Microsoft.VisualStudio.Services.Agent.Listener
{
    [SupportedOSPlatform("windows")]
    internal sealed class WindowsSingleBinaryService : ServiceBase
    {
        private const string ServiceChildEnvironmentVariable = "AZP_AGENT_SINGLE_BINARY_SERVICE_CHILD";
        private const string DefaultServiceName = "AzpAgentSingleBinaryService";
        private const string ServiceNameArgument = "--servicename";
        private readonly string[] _listenerArgs;
        private readonly object _lock = new object();
        private Process _listenerProcess;
        private Task _runLoop;
        private bool _stopping;

        private WindowsSingleBinaryService(string[] listenerArgs)
        {
            _listenerArgs = listenerArgs ?? Array.Empty<string>();
            ServiceName = GetServiceName(_listenerArgs);
            CanShutdown = true;
        }

        public static bool ShouldRunAsServiceHost(string[] args)
        {
            if (!OperatingSystem.IsWindows() || Environment.UserInteractive)
            {
                return false;
            }

            if (string.Equals(Environment.GetEnvironmentVariable(ServiceChildEnvironmentVariable), "1", StringComparison.Ordinal))
            {
                return false;
            }

            return HasRunCommand(args) && HasServiceStartupType(args);
        }

        public static int Run(string[] args)
        {
            using (var service = new WindowsSingleBinaryService(args))
            {
                ServiceBase.Run(service);
            }

            return Constants.Agent.ReturnCode.Success;
        }

        protected override void OnStart(string[] args)
        {
            _runLoop = Task.Run(RunListenerLoop);
        }

        protected override void OnStop()
        {
            lock (_lock)
            {
                _stopping = true;
                StopListenerProcess();
            }
        }

        protected override void OnShutdown()
        {
            OnStop();
            base.OnShutdown();
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                lock (_lock)
                {
                    _listenerProcess?.Dispose();
                    _listenerProcess = null;
                }
            }

            base.Dispose(disposing);
        }

        private void RunListenerLoop()
        {
            TimeSpan retryDelay = TimeSpan.FromSeconds(5);

            while (!IsStopping())
            {
                Process process = CreateListenerProcess();

                lock (_lock)
                {
                    if (_stopping)
                    {
                        process.Dispose();
                        return;
                    }

                    _listenerProcess = process;
                }

                try
                {
                    process.Start();
                    process.WaitForExit();

                    int exitCode = process.ExitCode;
                    if (exitCode == Constants.Agent.ReturnCode.Success ||
                        exitCode == Constants.Agent.ReturnCode.TerminatedError)
                    {
                        RequestServiceStop();
                        return;
                    }
                }
                catch
                {
                    if (IsStopping())
                    {
                        return;
                    }
                }
                finally
                {
                    lock (_lock)
                    {
                        _listenerProcess?.Dispose();
                        _listenerProcess = null;
                    }
                }

                if (!IsStopping())
                {
                    Thread.Sleep(retryDelay);
                }
            }
        }

        private Process CreateListenerProcess()
        {
            string executablePath = Environment.ProcessPath;
            if (string.IsNullOrEmpty(executablePath))
            {
                executablePath = Assembly.GetEntryAssembly()?.Location;
            }

            if (string.IsNullOrEmpty(executablePath))
            {
                throw new InvalidOperationException("Unable to determine Agent.Listener.exe path for Windows service host.");
            }

            var process = new Process();
            process.StartInfo = new ProcessStartInfo
            {
                FileName = executablePath,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                WorkingDirectory = Path.GetDirectoryName(executablePath) ?? string.Empty,
            };

            foreach (string argument in GetChildArguments(_listenerArgs))
            {
                process.StartInfo.ArgumentList.Add(argument);
            }

            process.StartInfo.Environment[ServiceChildEnvironmentVariable] = "1";
            return process;
        }

        private bool IsStopping()
        {
            lock (_lock)
            {
                return _stopping;
            }
        }

        private void StopListenerProcess()
        {
            try
            {
                if (_listenerProcess != null && !_listenerProcess.HasExited)
                {
                    _listenerProcess.Kill(entireProcessTree: true);
                }
            }
            catch
            {
                // SCM stop must not fail because the child process already exited or cannot be signaled.
            }
        }

        private void RequestServiceStop()
        {
            lock (_lock)
            {
                _stopping = true;
            }

            Stop();
        }

        private static bool HasRunCommand(string[] args)
        {
            return args?.Any(arg => string.Equals(arg, "run", StringComparison.OrdinalIgnoreCase)) == true;
        }

        private static bool HasServiceStartupType(string[] args)
        {
            if (args == null)
            {
                return false;
            }

            for (int i = 0; i < args.Length; i++)
            {
                string arg = args[i];
                if (string.Equals(arg, "--startuptype", StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(arg, "-startuptype", StringComparison.OrdinalIgnoreCase))
                {
                    return i + 1 < args.Length &&
                        string.Equals(args[i + 1], "service", StringComparison.OrdinalIgnoreCase);
                }

                if (arg.StartsWith("--startuptype=", StringComparison.OrdinalIgnoreCase) ||
                    arg.StartsWith("-startuptype=", StringComparison.OrdinalIgnoreCase))
                {
                    string value = arg.Substring(arg.IndexOf('=') + 1);
                    return string.Equals(value, "service", StringComparison.OrdinalIgnoreCase);
                }
            }

            return false;
        }

        private static string GetServiceName(string[] args)
        {
            if (args == null)
            {
                return DefaultServiceName;
            }

            for (int i = 0; i < args.Length; i++)
            {
                string arg = args[i];
                if (string.Equals(arg, ServiceNameArgument, StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(arg, "-servicename", StringComparison.OrdinalIgnoreCase))
                {
                    return i + 1 < args.Length && !string.IsNullOrWhiteSpace(args[i + 1])
                        ? args[i + 1]
                        : DefaultServiceName;
                }

                if (arg.StartsWith(ServiceNameArgument + "=", StringComparison.OrdinalIgnoreCase) ||
                    arg.StartsWith("-servicename=", StringComparison.OrdinalIgnoreCase))
                {
                    string value = arg.Substring(arg.IndexOf('=') + 1);
                    return !string.IsNullOrWhiteSpace(value) ? value : DefaultServiceName;
                }
            }

            return DefaultServiceName;
        }

        private static string[] GetChildArguments(string[] args)
        {
            if (args == null || args.Length == 0)
            {
                return Array.Empty<string>();
            }

            var childArgs = new System.Collections.Generic.List<string>();
            for (int i = 0; i < args.Length; i++)
            {
                string arg = args[i];
                if (string.Equals(arg, ServiceNameArgument, StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(arg, "-servicename", StringComparison.OrdinalIgnoreCase))
                {
                    i++;
                    continue;
                }

                if (arg.StartsWith(ServiceNameArgument + "=", StringComparison.OrdinalIgnoreCase) ||
                    arg.StartsWith("-servicename=", StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }

                childArgs.Add(arg);
            }

            return childArgs.ToArray();
        }
    }
}
