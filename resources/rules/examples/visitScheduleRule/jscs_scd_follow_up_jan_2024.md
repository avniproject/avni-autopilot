---
rule_kind: visitScheduleRule
intent: "if patient status wasn't recorded, schedule the next SCD Followup 90 days later with a 25-day window"
entity_param: programEncounter
encounter_types: ["SCD Followup Jan 2024"]
concepts: []
source_org: "JSCS"
---
```js
"use strict";
({ params, imports }) => {
  const programEncounter = params.entity;
  const moment = imports.moment;
  const scheduleBuilder = new imports.rulesConfig.VisitScheduleBuilder({programEncounter});
  
  const patientStatus = new imports.rulesConfig.RuleCondition({programEncounter}).when.valueInEncounter("e500ef46-7793-47f5-994a-359fd41948db").notDefined.matches();
  
  if( patientStatus ){
    const earliestDate = moment(programEncounter.encounterDateTime).add(90, 'days').toDate();
    const maxDate = moment(programEncounter.encounterDateTime).add(115, 'days').toDate();
    
    scheduleBuilder.add({name: "SCD Followup Jan 2024", encounterType: "SCD Followup Jan 2024", earliestDate, maxDate});  
}

  return scheduleBuilder.getAll();
};
```
