---
rule_kind: visitScheduleRule
intent: "schedule a Midline survey if menarche status indicates post-menarche, otherwise schedule a Pre-menarche visit — both on the registration date with a 4-day window, only if neither is already scheduled"
entity_param: individual
encounter_types: ["Midline survey", "Pre-menarche"]
concepts: []
source_org: "Periodshala"
---
```js
"use strict";
({ params, imports }) => {
  const individual = params.entity;
  const moment = imports.moment;
  const scheduleBuilder = new imports.rulesConfig.VisitScheduleBuilder({individual});
  
  console.log(individual);
  const editFlag = individual.encounters.filter((enc) => (enc.encounterType.name === "Pre-menarche" || enc.encounterType.name === "Midline survey")).length === 0;
  
  if(editFlag){
  
    const earliestDate = moment(individual.registrationDate).add(0, 'days').toDate();
    const maxDate = moment(individual.registrationDate).add(4, 'days').toDate();
    const condition11 = new imports.rulesConfig.RuleCondition({individual}).when.valueInRegistration("8d43845b-9847-42a3-98ca-a65ccd224e72").containsAnyAnswerConceptName("39cd5647-db76-4350-9801-72bfbf8ed5be","ba2cc1cb-6e48-4b37-8e86-4a7a260dcdf4","732b4aaf-c098-4f26-a09b-1a517626806f","b300511a-a720-407a-9207-8d3ecb9cf8ae").matches();
    
    if(condition11 ){
      scheduleBuilder.add({name: "Midline survey", encounterType: "Midline survey", earliestDate, maxDate});  
    }
    else{
      scheduleBuilder.add({name: "Pre-menarche", encounterType: "Pre-menarche", earliestDate, maxDate}); 
    }
  }
  
  return scheduleBuilder.getAll();
};
```
