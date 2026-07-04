package citysim;

import org.matsim.api.core.v01.Scenario;
import org.matsim.core.config.Config;
import org.matsim.core.config.ConfigUtils;
import org.matsim.core.controler.Controler;
import org.matsim.core.scenario.ScenarioUtils;
import org.matsim.pt.utils.CreatePseudoNetwork;

public final class RunCitySim {
    private RunCitySim() {
    }

    public static void main(String[] args) {
        String configPath = args.length > 0 ? args[0] : "../scenarios/logan_square/config.xml";
        Config config = ConfigUtils.loadConfig(configPath);
        Scenario scenario = ScenarioUtils.loadScenario(config);
        // Build a reserved pt pseudo-network from the GTFS schedule so transit
        // runs on its own links and never congests the car network. Adds link
        // refs + route link-lists that the Python GTFS writer intentionally omits.
        if (config.transit().isUseTransit() && !scenario.getTransitSchedule().getTransitLines().isEmpty()) {
            new CreatePseudoNetwork(scenario.getTransitSchedule(), scenario.getNetwork(), "tr_").createNetwork();
        }
        Controler controler = new Controler(scenario);
        controler.run();
    }
}
