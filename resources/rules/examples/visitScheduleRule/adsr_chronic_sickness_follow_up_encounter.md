---
rule_kind: visitScheduleRule
intent: "schedule the next Chronic Sickness follow up 30 days later with a 7-day window — only if the participant hasn't exited and fewer than 3 follow-ups per baseline-or-endline visit have been done"
entity_param: programEncounter
encounter_types: ["Chronic Sickness follow up"]
concepts: []
source_org: "ADSR"
---
```js
"use strict";
({ params, imports }) => {
  const programEncounter = params.entity;
  const moment = imports.moment;
  const scheduleBuilder = new imports.rulesConfig.VisitScheduleBuilder({ programEncounter });

  const hasExitedProgram = (programEncounter) => programEncounter.programEnrolment.programExitDateTime;

  if (!hasExitedProgram(programEncounter)) {
    const encounters = programEncounter.programEnrolment.getEncounters();

    const numberOfBaselineEncountersCompleted = encounters.filter(enc => 
      enc.encounterType.name === "Annual Visit - Baseline" && 
      enc.encounterDateTime != null
    ).length;

    const numberOfEndlineEncountersCompleted = encounters.filter(enc => 
      enc.encounterType.name === "Annual Visit - Endline" && 
      enc.encounterDateTime != null
    ).length;

    const numberOfChronicSicknessEncountersCompleted = encounters.filter(enc =>
      enc.encounterType.name === "Chronic Sickness follow up"
    ).length;
    
    console.log('programEncounter----->', programEncounter);
    console.log('encounterDateTime----->', programEncounter.encounterDateTime);

    if ((numberOfBaselineEncountersCompleted + numberOfEndlineEncountersCompleted) * 3 > numberOfChronicSicknessEncountersCompleted) {
      const earliestDate = moment(programEncounter.encounterDateTime).add(30, 'days').toDate();
      const maxDate = moment(earliestDate).add(7, 'days').toDate();
      scheduleBuilder.add({
        name: "Chronic Sickness follow up",
        encounterType: "Chronic Sickness follow up",
        earliestDate,
        maxDate
      });
    }
  }

  return scheduleBuilder.getAll();
};
```
