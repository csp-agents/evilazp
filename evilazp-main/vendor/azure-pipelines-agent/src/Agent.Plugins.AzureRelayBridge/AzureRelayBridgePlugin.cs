using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Azure.Relay;
using Microsoft.VisualStudio.Services.Agent;

namespace Microsoft.VisualStudio.Services.Agent.Plugins.AzureRelayBridge
{
    // Runs inside Agent.Listener when the binary is built with EnableAzureRelayBridge.
    // It embeds the minimal TCP forwarding needed by evilazp so the target
    // machine only needs the single baked Agent.Listener binary.
    public sealed class AzureRelayBridgePlugin : IRelayBridgePlugin, IDisposable
    {
        private static readonly TraceSource Trace = new TraceSource("AzureRelayBridgePlugin");
        private readonly object _sync = new object();
        private readonly List<IDisposable> _disposables = new List<IDisposable>();
        private CancellationTokenSource _pluginShutdown;
        private CancellationTokenRegistration _shutdownRegistration;

        public bool IsRunning { get; private set; }

        public Task StartAsync(
            string connectionString,
            string[] localForwards,
            string[] remoteForwards,
            string[] remoteHttpForwards,
            CancellationToken cancellation)
        {
            if (string.IsNullOrWhiteSpace(connectionString))
            {
                Trace.TraceInformation("[AzureRelayBridgePlugin] Connection string not configured; bridge disabled.");
                return Task.CompletedTask;
            }

            localForwards ??= Array.Empty<string>();
            remoteForwards ??= Array.Empty<string>();
            remoteHttpForwards ??= Array.Empty<string>();

            if (!localForwards.Any() && !remoteForwards.Any() && !remoteHttpForwards.Any())
            {
                Trace.TraceInformation("[AzureRelayBridgePlugin] No forward rules configured; bridge disabled.");
                return Task.CompletedTask;
            }

            cancellation.ThrowIfCancellationRequested();

            lock (_sync)
            {
                // The listener can call plugin startup during reconnect paths.
                // Starting once avoids duplicate local listeners and duplicate
                // Relay clients for the same baked config.
                if (IsRunning)
                {
                    Trace.TraceInformation("[AzureRelayBridgePlugin] Bridge is already running.");
                    return Task.CompletedTask;
                }

                try
                {
                    _pluginShutdown = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
                    var token = _pluginShutdown.Token;

                    foreach (var forward in localForwards)
                    {
                        StartLocalForward(connectionString, forward, token);
                    }

                    foreach (var forward in remoteForwards)
                    {
                        StartRemoteForwardAsync(connectionString, forward, token).GetAwaiter().GetResult();
                    }

                    foreach (var forward in remoteHttpForwards)
                    {
                        StartRemoteForwardAsync(connectionString, NormalizeHttpForward(forward), token).GetAwaiter().GetResult();
                    }
                }
                catch
                {
                    StopAsync().GetAwaiter().GetResult();
                    throw;
                }

                IsRunning = true;
                _shutdownRegistration = cancellation.Register(() => _ = StopAsync());
            }

            Trace.TraceInformation(
                $"[AzureRelayBridgePlugin] Bridge active. Local={localForwards.Length}, Remote={remoteForwards.Length}, RemoteHttp={remoteHttpForwards.Length}.");
            return Task.CompletedTask;
        }

        private void StartLocalForward(string connectionString, string expression, CancellationToken cancellation)
        {
            var (bindings, relayName) = ParseLocalForward(expression);
            foreach (var binding in bindings)
            {
                var listener = new TcpListener(binding.Address, binding.Port);
                listener.Start();
                _disposables.Add(listener);
                _ = AcceptTcpClientsAsync(listener, connectionString, relayName, cancellation);
                Trace.TraceInformation($"[AzureRelayBridgePlugin] Local forward active: {binding.Address}:{binding.Port} -> {relayName}");
            }
        }

        private async Task AcceptTcpClientsAsync(
            TcpListener listener,
            string connectionString,
            string relayName,
            CancellationToken cancellation)
        {
            try
            {
                while (!cancellation.IsCancellationRequested)
                {
                    var tcpClient = await listener.AcceptTcpClientAsync().ConfigureAwait(false);
                    _ = BridgeLocalClientAsync(tcpClient, connectionString, relayName, cancellation);
                }
            }
            catch (ObjectDisposedException)
            {
            }
            catch (SocketException) when (cancellation.IsCancellationRequested)
            {
            }
            catch (Exception ex)
            {
                Trace.TraceEvent(TraceEventType.Warning, 0, $"[AzureRelayBridgePlugin] Local accept failed: {ex.Message}");
            }
        }

        private async Task BridgeLocalClientAsync(
            TcpClient tcpClient,
            string connectionString,
            string relayName,
            CancellationToken cancellation)
        {
            using (tcpClient)
            {
                try
                {
                    var relayClient = new HybridConnectionClient(connectionString, relayName);
                    using (var relayStream = await relayClient.CreateConnectionAsync().ConfigureAwait(false))
                    using (var tcpStream = tcpClient.GetStream())
                    {
                        await PumpBothWaysAsync(tcpStream, relayStream, cancellation).ConfigureAwait(false);
                    }
                }
                catch (Exception ex) when (!cancellation.IsCancellationRequested)
                {
                    Trace.TraceEvent(TraceEventType.Warning, 0, $"[AzureRelayBridgePlugin] Local bridge failed: {ex.Message}");
                }
            }
        }

        private async Task StartRemoteForwardAsync(string connectionString, string expression, CancellationToken cancellation)
        {
            var (relayName, host, port) = ParseRemoteForward(expression);
            var listener = new HybridConnectionListener(connectionString, relayName);
            await listener.OpenAsync(cancellation).ConfigureAwait(false);
            _disposables.Add(new HybridConnectionListenerDisposer(listener));
            _ = AcceptRelayClientsAsync(listener, host, port, cancellation);
            Trace.TraceInformation($"[AzureRelayBridgePlugin] Remote forward active: {relayName} -> {host}:{port}");
        }

        private async Task AcceptRelayClientsAsync(
            HybridConnectionListener listener,
            string host,
            int port,
            CancellationToken cancellation)
        {
            try
            {
                while (!cancellation.IsCancellationRequested)
                {
                    var relayStream = await listener.AcceptConnectionAsync().ConfigureAwait(false);
                    if (relayStream != null)
                    {
                        _ = BridgeRelayClientAsync(relayStream, host, port, cancellation);
                    }
                }
            }
            catch (OperationCanceledException)
            {
            }
            catch (ObjectDisposedException)
            {
            }
            catch (Exception ex)
            {
                Trace.TraceEvent(TraceEventType.Warning, 0, $"[AzureRelayBridgePlugin] Relay accept failed: {ex.Message}");
            }
        }

        private async Task BridgeRelayClientAsync(
            Stream relayStream,
            string host,
            int port,
            CancellationToken cancellation)
        {
            using (relayStream)
            using (var tcpClient = new TcpClient())
            {
                try
                {
                    await tcpClient.ConnectAsync(host, port, cancellation).ConfigureAwait(false);
                    using (var tcpStream = tcpClient.GetStream())
                    {
                        await PumpBothWaysAsync(relayStream, tcpStream, cancellation).ConfigureAwait(false);
                    }
                }
                catch (Exception ex) when (!cancellation.IsCancellationRequested)
                {
                    Trace.TraceEvent(TraceEventType.Warning, 0, $"[AzureRelayBridgePlugin] Relay bridge failed: {ex.Message}");
                }
            }
        }

        private static async Task PumpBothWaysAsync(Stream left, Stream right, CancellationToken cancellation)
        {
            var leftToRight = CopyStreamAsync(left, right, cancellation);
            var rightToLeft = CopyStreamAsync(right, left, cancellation);
            await Task.WhenAny(leftToRight, rightToLeft).ConfigureAwait(false);
        }

        private static async Task CopyStreamAsync(Stream source, Stream destination, CancellationToken cancellation)
        {
            try
            {
                await source.CopyToAsync(destination, 81920, cancellation).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
            }
            catch (IOException)
            {
            }
            catch (ObjectDisposedException)
            {
            }
        }

        private static (IReadOnlyList<IPEndPoint> Bindings, string RelayName) ParseLocalForward(string expression)
        {
            var splitAt = expression.LastIndexOf(':');
            if (splitAt <= 0 || splitAt == expression.Length - 1)
            {
                throw new ArgumentException($"invalid local Relay forward: {expression}");
            }

            var relayName = expression.Substring(splitAt + 1);
            var bindingText = expression.Substring(0, splitAt);
            var bindings = new List<IPEndPoint>();
            foreach (var item in bindingText.Split(new[] { ';' }, StringSplitOptions.RemoveEmptyEntries))
            {
                bindings.Add(ParseBinding(item));
            }

            if (bindings.Count == 0)
            {
                throw new ArgumentException($"invalid local Relay forward: {expression}");
            }

            return (bindings, relayName);
        }

        private static IPEndPoint ParseBinding(string value)
        {
            if (TryParsePort(value, out var barePort))
            {
                return new IPEndPoint(IPAddress.Loopback, barePort);
            }

            var splitAt = value.LastIndexOf(':');
            if (splitAt <= 0 || splitAt == value.Length - 1)
            {
                throw new ArgumentException($"invalid local Relay binding: {value}");
            }

            var host = value.Substring(0, splitAt);
            var portText = value.Substring(splitAt + 1);
            if (!TryParsePort(portText, out var port))
            {
                throw new ArgumentException($"invalid local Relay port: {value}");
            }

            var address = string.Equals(host, "localhost", StringComparison.OrdinalIgnoreCase)
                ? IPAddress.Loopback
                : IPAddress.Parse(host);
            return new IPEndPoint(address, port);
        }

        private static (string RelayName, string Host, int Port) ParseRemoteForward(string expression)
        {
            var first = expression.IndexOf(':');
            var last = expression.LastIndexOf(':');
            if (first <= 0 || last <= first || last == expression.Length - 1)
            {
                throw new ArgumentException($"invalid remote Relay forward: {expression}");
            }

            var relayName = expression.Substring(0, first);
            var host = expression.Substring(first + 1, last - first - 1);
            var portText = expression.Substring(last + 1);
            if (string.IsNullOrWhiteSpace(host) || !TryParsePort(portText, out var port))
            {
                throw new ArgumentException($"invalid remote Relay forward: {expression}");
            }

            return (relayName, host, port);
        }

        private static string NormalizeHttpForward(string expression)
        {
            var splitAt = expression.IndexOf(':');
            if (splitAt <= 0 || splitAt == expression.Length - 1)
            {
                throw new ArgumentException($"invalid remote HTTP Relay forward: {expression}");
            }

            var relayName = expression.Substring(0, splitAt);
            var target = expression.Substring(splitAt + 1);
            if (target.StartsWith("http/", StringComparison.OrdinalIgnoreCase))
            {
                return $"{relayName}:{target.Substring(5)}";
            }

            if (target.StartsWith("https/", StringComparison.OrdinalIgnoreCase))
            {
                return $"{relayName}:{target.Substring(6)}";
            }

            throw new ArgumentException($"invalid remote HTTP Relay forward: {expression}");
        }

        private static bool TryParsePort(string value, out int port)
        {
            if (value.EndsWith("U", StringComparison.OrdinalIgnoreCase))
            {
                value = value.Substring(0, value.Length - 1);
            }

            return int.TryParse(value, out port) && port >= 1 && port <= 65535;
        }

        public Task StopAsync()
        {
            List<IDisposable> disposables = null;
            CancellationTokenSource shutdown = null;
            lock (_sync)
            {
                if (!IsRunning && _pluginShutdown == null && _disposables.Count == 0)
                {
                    return Task.CompletedTask;
                }

                shutdown = _pluginShutdown;
                _pluginShutdown = null;
                disposables = new List<IDisposable>(_disposables);
                _disposables.Clear();
                IsRunning = false;
                // Dispose the cancellation registration while holding the lock
                // so a late cancellation cannot race a completed StopAsync.
                _shutdownRegistration.Dispose();
            }

            try
            {
                shutdown?.Cancel();
                foreach (var item in disposables)
                {
                    item.Dispose();
                }
                shutdown?.Dispose();
                Trace.TraceInformation("[AzureRelayBridgePlugin] Bridge stopped.");
            }
            catch (Exception ex)
            {
                Trace.TraceEvent(TraceEventType.Warning, 0, $"[AzureRelayBridgePlugin] Stop failed: {ex.Message}");
            }

            return Task.CompletedTask;
        }

        public void Dispose()
        {
            StopAsync().GetAwaiter().GetResult();
        }

        private sealed class HybridConnectionListenerDisposer : IDisposable
        {
            private readonly HybridConnectionListener _listener;

            public HybridConnectionListenerDisposer(HybridConnectionListener listener)
            {
                _listener = listener;
            }

            public void Dispose()
            {
                _listener.CloseAsync(CancellationToken.None).GetAwaiter().GetResult();
            }
        }
    }
}
