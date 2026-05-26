using System.Configuration;
using System.Web.Mvc;

namespace LegacyWebApp.Controllers
{
    public class HomeController : Controller
    {
        public string Index()
        {
            return ConfigurationManager.AppSettings["Greeting"];
        }
    }
}
