using System.Threading;
using System.Threading.Tasks;

namespace Microsoft.VisualStudio.Services.Agent
{
    public interface ITunnelPlugin
    {
        string TunnelUrl { get; }
        Task StartAsync(string tenantId, string clientId, string clientSecret, string tunnelId, int[] ports, CancellationToken cancellation);
        Task StopAsync();
    }
}
