// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

using Agent.Sdk;
using Microsoft.VisualStudio.Services.Agent.Util;
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Linq;

namespace Microsoft.VisualStudio.Services.Agent
{
    [ServiceLocator(Default = typeof(ExtensionManager))]
    public interface IExtensionManager : IAgentService
    {
        List<T> GetExtensions<T>() where T : class, IExtension;
    }

    public sealed class ExtensionManager : AgentService, IExtensionManager
    {
        private readonly ConcurrentDictionary<Type, List<IExtension>> _cache = new ConcurrentDictionary<Type, List<IExtension>>();

        public List<T> GetExtensions<T>() where T : class, IExtension
        {
            Trace.Info("Getting extensions for interface: '{0}'", typeof(T).FullName);
            List<IExtension> extensions = _cache.GetOrAdd(
                key: typeof(T),
                valueFactory: (Type key) =>
                {
                    return LoadExtensions<T>();
                });
            return extensions.Select(x => x as T).ToList();
        }

        //
        // We will load extensions from assembly
        // once AssemblyLoadContext.Resolving event is able to
        // resolve dependency recursively
        //
        private List<IExtension> LoadExtensions<T>() where T : class, IExtension
        {
            var extensions = new List<IExtension>();
            switch (typeof(T).FullName)
            {
                // Listener capabilities providers.
                case "Microsoft.VisualStudio.Services.Agent.Capabilities.ICapabilitiesProvider":
                    Add<T>(extensions, "Microsoft.VisualStudio.Services.Agent.Capabilities.AgentCapabilitiesProvider, Microsoft.VisualStudio.Services.Agent");
                    Add<T>(extensions, "Microsoft.VisualStudio.Services.Agent.Capabilities.EnvironmentCapabilitiesProvider, Microsoft.VisualStudio.Services.Agent");
                    if (PlatformUtil.RunningOnLinux || PlatformUtil.RunningOnMacOS)
                    {
                        Add<T>(extensions, "Microsoft.VisualStudio.Services.Agent.Capabilities.NixCapabilitiesProvider, Microsoft.VisualStudio.Services.Agent");
                    }
                    if (PlatformUtil.RunningOnWindows)
                    {
                        Add<T>(extensions, "Microsoft.VisualStudio.Services.Agent.Capabilities.PowerShellCapabilitiesProvider, Microsoft.VisualStudio.Services.Agent");
                    }
                    Add<T>(extensions, "Microsoft.VisualStudio.Services.Agent.Capabilities.UserCapabilitiesProvider, Microsoft.VisualStudio.Services.Agent");
                    break;
                // Listener agent configuration providers
                case "Microsoft.VisualStudio.Services.Agent.Listener.Configuration.IConfigurationProvider":
                    Add<T>(extensions, "Microsoft.VisualStudio.Services.Agent.Listener.Configuration.BuildReleasesAgentConfigProvider, Agent.Listener");
                    Add<T>(extensions, "Microsoft.VisualStudio.Services.Agent.Listener.Configuration.DeploymentGroupAgentConfigProvider, Agent.Listener");
                    Add<T>(extensions, "Microsoft.VisualStudio.Services.Agent.Listener.Configuration.SharedDeploymentAgentConfigProvider, Agent.Listener");
                    Add<T>(extensions, "Microsoft.VisualStudio.Services.Agent.Listener.Configuration.EnvironmentVMResourceConfigProvider, Agent.Listener");
                    break;
                // Worker job extensions.
                case "Microsoft.VisualStudio.Services.Agent.Worker.IJobExtension":
                    Add<T>(extensions, "Microsoft.VisualStudio.Services.Agent.Worker.Build.BuildJobExtension, Agent.Worker");
                    break;
                // Worker command extensions.
                case "Microsoft.VisualStudio.Services.Agent.Worker.IWorkerCommandExtension":
                    Add<T>(extensions, "Microsoft.VisualStudio.Services.Agent.Worker.TaskCommandExtension, Agent.Worker");
                    Add<T>(extensions, "Microsoft.VisualStudio.Services.Agent.Worker.Telemetry.TelemetryCommandExtension, Agent.Worker");
                    break;
                // Worker build source providers.
                case "Microsoft.VisualStudio.Services.Agent.Worker.Build.ISourceProvider":
                    break;
                // Worker release artifact extensions.
                case "Microsoft.VisualStudio.Services.Agent.Worker.Release.IArtifactExtension":
                    break;
                // Worker test result readers.
                case "Microsoft.VisualStudio.Services.Agent.Worker.LegacyTestResults.IResultReader":
                    break;
                // Worker test result parser.
                case "Microsoft.VisualStudio.Services.Agent.Worker.TestResults.IParser":
                    break;
                // Worker code coverage summary reader extensions.
                case "Microsoft.VisualStudio.Services.Agent.Worker.CodeCoverage.ICodeCoverageSummaryReader":
                    break;
                // Worker maintenance service provider extensions.
                case "Microsoft.VisualStudio.Services.Agent.Worker.Maintenance.IMaintenanceServiceProvider":
                    break;
                default:
                    // This should never happen.
                    throw new NotSupportedException($"Unexpected extension type: '{typeof(T).FullName}'");
            }

            return extensions;
        }

        private void Add<T>(List<IExtension> extensions, string assemblyQualifiedName) where T : class, IExtension
        {
            Trace.Info($"Creating instance: {assemblyQualifiedName}");
            Type type = Type.GetType(assemblyQualifiedName, throwOnError: true);
            var extension = Activator.CreateInstance(type) as T;
            ArgUtil.NotNull(extension, nameof(extension));
            extension.Initialize(HostContext);
            extensions.Add(extension);
        }
    }
}
