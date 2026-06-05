---
rule_kind: visitScheduleRule
intent: "schedule the first Volunteer Monthly Report for the first of next month with a 25-day window"
entity_param: individual
encounter_types: ["Volunteer Monthly Report"]
concepts: []
source_org: "Cini"
---
```js
"use strict";
({ params, imports }) => {
    const individual = params.entity;
    const moment = imports.moment;
    const scheduleBuilder = new imports.rulesConfig.VisitScheduleBuilder({
        individual
    });

    const isDistrictTeamCapacityBuildingEncounterAlreadyPresent = individual.getEncounters().filter(enc =>
        enc.name === 'Volunteer Monthly Report' && enc.encounterDateTime && !enc.cancelDateTime && !enc.voided
    ).length > 0;

    if (!isDistrictTeamCapacityBuildingEncounterAlreadyPresent) {
        const firstDayOfNextMonth = moment(individual.registrationDate).add(1, 'months').startOf('month');
        const earliestDate = firstDayOfNextMonth.toDate();
        const maxDate = firstDayOfNextMonth.add(25, 'days').toDate();
        scheduleBuilder.add({name: "Volunteer Monthly Report", encounterType: "Volunteer Monthly Report", earliestDate, maxDate});
    }

    return scheduleBuilder.getAll();
};
```
