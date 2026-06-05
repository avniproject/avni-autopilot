---
rule_kind: visitScheduleRule
intent: "schedule a Sickle cell testing entry one month before the registration date with a 45-day window, named 'Sickle cell testing entry for <month year>'"
entity_param: individual
encounter_types: ["Sickle cell testing entry"]
concepts: []
source_org: "JSCS"
---
```js
"use strict";
({params, imports}) => {
    const individual = params.entity;
    const scheduleBuilder = new imports.rulesConfig.VisitScheduleBuilder({individual});
    const earliestDate = imports.moment(individual.registrationDate).subtract(1,'months').toDate();
    const maxDate = imports.moment(earliestDate).add(45, 'd').toDate();
    const nameSufix = imports.moment(earliestDate).format("MMM YYYY");
    scheduleBuilder.add({
            name: `Sickle cell testing entry for ${nameSufix}`,
            encounterType: 'Sickle cell testing entry',
            earliestDate,
            maxDate
        });
    return scheduleBuilder.getAllUnique("encounterType");
};
```
