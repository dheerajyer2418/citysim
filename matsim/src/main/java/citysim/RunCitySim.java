package citysim;

import org.matsim.api.core.v01.Scenario;
import org.matsim.core.config.Config;
import org.matsim.core.config.ConfigUtils;
import org.matsim.core.controler.Controler;
import org.matsim.core.scenario.ScenarioUtils;

public final class RunCitySim {
    private RunCitySim() {
    }

    public static void main(String[] args) {
        String configPath = args.length > 0 ? args[0] : "../scenarios/logan_square/config.xml";
        Config config = ConfigUtils.loadConfig(configPath);
        Scenario scenario = ScenarioUtils.loadScenario(config);
        Controler controler = new Controler(scenario);
        controler.run();
    }
}
