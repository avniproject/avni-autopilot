---
rule_kind: visitScheduleRule
intent: "schedule the following month's Library Monthly Report on the first of next month with a 25-day window"
entity_param: encounter
encounter_types: ["Library Monthly Report"]
concepts: []
source_org: "Cini"
---
```js
"use strict";
({params, imports}) => {
    const encounter = params.entity;
    const moment = imports.moment;
    const scheduleBuilder = new imports.rulesConfig.VisitScheduleBuilder({encounter});

    const encounters = encounter.individual.getEncounters();

    const currentEncounterVisitMonthYear = moment(encounter.earliestVisitDateTime).format('MM-YYYY');

    const isLibraryMonthlyReportEncounterAlreadyPresent = encounters.some(enc => !enc.voided && enc.name === 'Library Monthly Report' && enc.encounterDateTime && moment(enc.encounterDateTime).format('MM-YYYY') === currentEncounterVisitMonthYear && !enc.cancelDateTime);

    if (!isLibraryMonthlyReportEncounterAlreadyPresent) {
        const firstDayOfNextMonth = moment(encounter.earliestVisitDateTime).add(1, 'months').startOf('month');
        const earliestDate = firstDayOfNextMonth.toDate();
        const maxDate = firstDayOfNextMonth.add(25, 'days').toDate();
        scheduleBuilder.add({
            name: "Library Monthly Report", encounterType: "Library Monthly Report", earliestDate, maxDate
        });
    }

    return scheduleBuilder.getAll();
};
```
